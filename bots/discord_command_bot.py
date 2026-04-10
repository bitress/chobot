"""
Discord Command Bot Module
Handles Discord commands for item and villager search with rich embeds
"""

import asyncio
import os
import sqlite3
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
from utils.helpers import normalize_text, get_best_suggestions, clean_text
from utils.nookipedia import NookipediaClient
from utils.chopaeng_ai import get_ai_answer, conversation_store, add_chat_message

logger = logging.getLogger("DiscordCommandBot")

# Island status check constants
DODO_CODE_PATTERN = re.compile(r'\b[A-HJ-NP-Z0-9]{5}\b')
MENTION_PATTERN = re.compile(r'<@!?\d+>')
ISLAND_HOST_NAME = "chopaeng"
MESSAGE_HISTORY_LIMIT = 30
ISLAND_DOWN_IMAGE_URL = "https://cdn.chopaeng.com/misc/Bot-is-Down.jpg"

# Patterns for intercepting island bot responses
ISLAND_VISITORS_PATTERN = re.compile(r"The following visitors are on (.+?):", re.IGNORECASE)
ISLAND_DODO_SENT_PATTERN = re.compile(r".+?:\s*Sent you the dodo code via DM", re.IGNORECASE)
VISITOR_LINE_PATTERN = re.compile(r'#\d+:\s*(.+)')
AVAILABLE_SLOT_TEXT = "available slot"
ISLAND_BOT_INTERCEPT_TIMEOUT = 10  # seconds to wait for island bot response
GIT_OUTPUT_MAX_LENGTH = 1900  # max chars of git output to display in Discord
DODO_XLOG_TIMEOUT = 1800  # seconds to wait for a verified flight before posting the dodo-request xlog
TOPIC_SYNC_INTERVAL_SECONDS = 30

# How long (seconds) a command claim record is kept before being pruned.
# Any message older than this window is no longer at risk of being replayed.
COMMAND_CLAIM_EXPIRY_SECONDS = 300  # 5 minutes

# Trivia game settings
TRIVIA_TIMEOUT = 30  # seconds before revealing the answer automatically

# ACNH trivia question bank — (question, [choice_A, B, C, D], correct_index 0-based)
ACNH_TRIVIA_QUESTIONS: list[dict] = [
    {"q": "What species is Marshall?",
     "c": ["Hamster", "Squirrel", "Cat", "Rabbit"], "a": 1},
    {"q": "Which personality type does Raymond have?",
     "c": ["Lazy", "Cranky", "Smug", "Jock"], "a": 2},
    {"q": "What is the name of the airport attendant in ACNH?",
     "c": ["Tom Nook", "Orville", "Dodo", "Isabelle"], "a": 1},
    {"q": "Which villager is known for the catchphrase 'kerplunk'?",
     "c": ["Bob", "Lucky", "Marshal", "Stitches"], "a": 2},
    {"q": "What item do you need to terraform your island in ACNH?",
     "c": ["Golden Shovel", "Island Designer App", "Pro Membership", "Ladder"], "a": 1},
    {"q": "Who is the shopkeeper at Nook's Cranny?",
     "c": ["Tom Nook", "Timmy & Tommy", "Label", "Leif"], "a": 1},
    {"q": "What species is Isabelle?",
     "c": ["Dog", "Cat", "Shih Tzu", "Rabbit"], "a": 0},
    {"q": "What personality type does Stitches have?",
     "c": ["Normal", "Peppy", "Lazy", "Smug"], "a": 2},
    {"q": "Which fruit is NOT a starting fruit in ACNH?",
     "c": ["Apples", "Pears", "Durian", "Oranges"], "a": 2},
    {"q": "What species is Ankha?",
     "c": ["Dog", "Rabbit", "Cat", "Bear"], "a": 2},
    {"q": "What type of item is the Golden Axe?",
     "c": ["Tool", "Furniture", "Clothing", "Fossil"], "a": 0},
    {"q": "What day does K.K. Slider perform on?",
     "c": ["Friday", "Saturday", "Sunday", "Monday"], "a": 1},
    {"q": "Which personality type is exclusive to male villagers in ACNH?",
     "c": ["Lazy", "Cranky", "Smug", "Jock"], "a": 1},
    {"q": "What species is Bob?",
     "c": ["Bear", "Cat", "Dog", "Frog"], "a": 1},
    {"q": "Which fruit does NOT grow natively on mystery islands (Nook Miles Tickets)?",
     "c": ["Cherries", "Pears", "Coconuts", "Durians"], "a": 3},
    {"q": "What do you use to catch bugs in ACNH?",
     "c": ["Fishing Rod", "Net", "Bug Trap", "Shovel"], "a": 1},
    {"q": "Which character runs the Able Sisters tailor shop?",
     "c": ["Mabel & Sable", "Celeste", "Label", "Harriet"], "a": 0},
    {"q": "What species is Goldie?",
     "c": ["Horse", "Rabbit", "Dog", "Cat"], "a": 2},
    {"q": "How many personality types exist in ACNH for female villagers?",
     "c": ["2", "3", "4", "5"], "a": 2},
    {"q": "What species is Merengue?",
     "c": ["Bear", "Rhino", "Hippo", "Dog"], "a": 1},
    {"q": "What material do you need to craft a Simple DIY Workbench?",
     "c": ["Iron Nuggets", "Wood only", "Stone + Wood", "Gold Nuggets"], "a": 1},
    {"q": "What species is Judy?",
     "c": ["Bear Cub", "Koala", "Hamster", "Cat"], "a": 0},
    {"q": "Which event features shooting stars you can wish on?",
     "c": ["Fishing Tourney", "Bug-Off", "Meteor Shower", "Harvest Festival"], "a": 2},
    {"q": "What item does Celeste give you during a meteor shower?",
     "c": ["Star Fragment", "Magic Wand Recipe", "DIY Recipe", "Shooting Star Wand"], "a": 2},
    {"q": "How many villagers can live on your island at once?",
     "c": ["8", "10", "12", "15"], "a": 1},
    {"q": "What species is Lucky?",
     "c": ["Cat", "Dog", "Bear", "Wolf"], "a": 1},
    {"q": "Which character hosts the Fishing Tourney?",
     "c": ["Blathers", "C.J.", "Flick", "Chip"], "a": 1},
    {"q": "Which character buys bugs at a premium during the Bug-Off?",
     "c": ["C.J.", "Flick", "Nat", "Pascal"], "a": 1},
    {"q": "What species is Marshal?",
     "c": ["Bear", "Hamster", "Squirrel", "Mouse"], "a": 2},
    {"q": "What do Star Fragments primarily come from?",
     "c": ["Fossils", "Meteor Showers", "Balloon Presents", "Diving"], "a": 1},
    {"q": "How many iron nuggets does it take to build Nook's Cranny?",
     "c": ["10", "20", "30", "40"], "a": 2},
    {"q": "What species is Fauna?",
     "c": ["Rabbit", "Deer", "Koala", "Bear"], "a": 1},
    {"q": "Which island facility is unlocked last by default?",
     "c": ["Museum", "Nook's Cranny", "Resident Services Building", "Able Sisters"], "a": 3},
    {"q": "What is the maximum number of stars you can wish on in one meteor shower night?",
     "c": ["10", "20", "Unlimited", "50"], "a": 2},
    {"q": "What personality type is Peppy?",
     "c": ["Male", "Female", "Both", "Rare"], "a": 1},
    {"q": "What species is Zucker?",
     "c": ["Frog", "Bear", "Octopus", "Cat"], "a": 2},
    {"q": "Which ACNH character can identify fossils?",
     "c": ["Tom Nook", "Blathers", "Isabelle", "Celeste"], "a": 1},
    {"q": "What are the two types of turnips in ACNH?",
     "c": ["Red & White", "White & Yellow", "Purple & White", "Golden & White"], "a": 0},
    {"q": "What is Chopaeng known for in the ACNH community?",
     "c": ["Speedrunning", "Hosting 24/7 treasure islands", "Drawing fan art", "Making mods"], "a": 1},
    {"q": "What command do you type to get a Dodo code on a Chopaeng sub island?",
     "c": ["!dodo", "!senddodo", "!code", "!sd — same as !senddodo"], "a": 3},
]

# Shared SQLite database path (project root)
_DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "chobot.db")


def _upsert_bot_status(island_id: str, island_name: str, is_online: bool) -> None:
    """Persist the Discord bot online/offline status for an island to the DB.

    Writes to the ``island_bot_status`` table so that the REST API can expose
    live Discord presence data without making Discord API calls itself.
    """
    try:
        conn = sqlite3.connect(_DB_PATH)
        try:
            conn.execute(
                """INSERT INTO island_bot_status (island_id, island_name, is_online, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(island_id) DO UPDATE SET
                       island_name=excluded.island_name,
                       is_online=excluded.is_online,
                       updated_at=excluded.updated_at""",
                (island_id, island_name, 1 if is_online else 0, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.error(f"[DISCORD] Failed to write island_bot_status for {island_name}: {exc}")


def _init_command_claims_db() -> None:
    """Create the command_claims table used for cross-instance deduplication."""
    try:
        with sqlite3.connect(_DB_PATH, timeout=5) as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS command_claims (
                    message_id INTEGER PRIMARY KEY,
                    claimed_at REAL NOT NULL
                )"""
            )
    except Exception as exc:
        logger.error(f"[DISCORD] Failed to init command_claims table: {exc}")


def _init_subscriptions_db() -> None:
    """Create the island_subscriptions table for online/offline alert opt-ins."""
    try:
        with sqlite3.connect(_DB_PATH, timeout=5) as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS island_subscriptions (
                    user_id INTEGER NOT NULL,
                    island_clean TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'sub',
                    PRIMARY KEY (user_id, island_clean, kind)
                )"""
            )
    except Exception as exc:
        logger.error(f"[DISCORD] Failed to init island_subscriptions table: {exc}")


def _add_subscription(user_id: int, island_clean: str, kind: str) -> bool:
    """Subscribe *user_id* to alerts for *island_clean*.

    Returns True if a new row was inserted, False if it already existed.
    """
    try:
        with sqlite3.connect(_DB_PATH, timeout=5) as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO island_subscriptions (user_id, island_clean, kind) VALUES (?, ?, ?)",
                (user_id, island_clean, kind),
            )
            return cursor.rowcount > 0
    except Exception as exc:
        logger.error(f"[DISCORD] Failed to add subscription {user_id}/{island_clean}: {exc}")
        return False


def _remove_subscription(user_id: int, island_clean: str | None) -> int:
    """Remove subscription(s) for *user_id*.

    If *island_clean* is None, all subscriptions for the user are removed.
    Returns the number of rows deleted.
    """
    try:
        with sqlite3.connect(_DB_PATH, timeout=5) as conn:
            if island_clean is None:
                cursor = conn.execute(
                    "DELETE FROM island_subscriptions WHERE user_id = ?",
                    (user_id,),
                )
            else:
                cursor = conn.execute(
                    "DELETE FROM island_subscriptions WHERE user_id = ? AND island_clean = ?",
                    (user_id, island_clean),
                )
            return cursor.rowcount
    except Exception as exc:
        logger.error(f"[DISCORD] Failed to remove subscription {user_id}/{island_clean}: {exc}")
        return 0


def _get_user_subscriptions(user_id: int) -> list[tuple[str, str]]:
    """Return a list of (island_clean, kind) tuples the user is subscribed to."""
    try:
        with sqlite3.connect(_DB_PATH, timeout=5) as conn:
            rows = conn.execute(
                "SELECT island_clean, kind FROM island_subscriptions WHERE user_id = ? ORDER BY island_clean",
                (user_id,),
            ).fetchall()
            return rows
    except Exception as exc:
        logger.error(f"[DISCORD] Failed to fetch subscriptions for {user_id}: {exc}")
        return []


def _get_island_subscribers(island_clean: str) -> list[int]:
    """Return a list of user_ids subscribed to alerts for *island_clean*."""
    try:
        with sqlite3.connect(_DB_PATH, timeout=5) as conn:
            rows = conn.execute(
                "SELECT user_id FROM island_subscriptions WHERE island_clean = ?",
                (island_clean,),
            ).fetchall()
            return [r[0] for r in rows]
    except Exception as exc:
        logger.error(f"[DISCORD] Failed to fetch subscribers for {island_clean}: {exc}")
        return []


def _try_claim_command(message_id: int) -> bool:
    """Attempt to claim a message ID for command processing.

    Uses a SQLite unique constraint so that only one bot instance (or one
    invocation within the same instance) can process a given Discord message.

    Returns True if this call is the first to claim the message (caller should
    proceed), False if it was already claimed (caller should skip).
    On any database error, returns True so the command is never silently lost.
    """
    try:
        now = time.time()
        with sqlite3.connect(_DB_PATH, timeout=5) as conn:
            conn.execute(
                "DELETE FROM command_claims WHERE claimed_at < ?",
                (now - COMMAND_CLAIM_EXPIRY_SECONDS,),
            )
            # INSERT OR IGNORE silently does nothing when the PRIMARY KEY already
            # exists (i.e. another instance already claimed this message_id).
            # cursor.rowcount is 1 on a successful insert and 0 on a no-op, so
            # it reliably distinguishes "first claim" from "duplicate".
            cursor = conn.execute(
                "INSERT OR IGNORE INTO command_claims (message_id, claimed_at) VALUES (?, ?)",
                (message_id, now),
            )
            return cursor.rowcount > 0
    except Exception as exc:
        logger.error(f"[DISCORD] command_claims check failed for {message_id}: {exc}")
        return True


def _discord_conv_key(message: discord.Message) -> str:
    """Return a stable per-user-per-channel key for conversation history."""
    guild_id = message.guild.id if message.guild else "dm"
    return f"discord:{guild_id}:{message.channel.id}:{message.author.id}"


# ---------------------------------------------------------------------------
# Trivia UI
# ---------------------------------------------------------------------------

_TRIVIA_LETTER = ["🇦", "🇧", "🇨", "🇩"]


class TriviaView(discord.ui.View):
    """Multiple-choice trivia buttons for a single ACNH question.

    The first user to click the correct answer wins.  After *timeout* seconds,
    or once any button is clicked, all buttons are disabled and the result is
    revealed.
    """

    def __init__(self, question: dict, timeout: int = TRIVIA_TIMEOUT):
        super().__init__(timeout=timeout)
        self.question = question
        self.answered = False

        for idx, choice in enumerate(question["c"]):
            label = f"{_TRIVIA_LETTER[idx]} {choice}"
            btn = discord.ui.Button(
                label=label,
                custom_id=str(idx),
                style=discord.ButtonStyle.secondary,
                row=0 if idx < 2 else 1,
            )
            btn.callback = self._make_callback(idx)
            self.add_item(btn)

    def _make_callback(self, idx: int):
        async def callback(interaction: discord.Interaction):
            if self.answered:
                await interaction.response.defer()
                return
            self.answered = True
            correct = self.question["a"]
            self._update_buttons(correct, chosen=idx)
            self.stop()

            if idx == correct:
                result_text = (
                    f"✅ **{interaction.user.display_name}** got it! "
                    f"The answer is **{self.question['c'][correct]}**! 🎉"
                )
            else:
                result_text = (
                    f"❌ **{interaction.user.display_name}** answered "
                    f"**{self.question['c'][idx]}**, but the correct answer is "
                    f"**{self.question['c'][correct]}**."
                )

            await interaction.response.edit_message(view=self)
            await interaction.followup.send(result_text)

        return callback

    def _update_buttons(self, correct: int, chosen: int | None = None) -> None:
        """Colour and disable all buttons."""
        for item in self.children:
            if not isinstance(item, discord.ui.Button):
                continue
            btn_idx = int(item.custom_id)
            if btn_idx == correct:
                item.style = discord.ButtonStyle.success
            elif chosen is not None and btn_idx == chosen and chosen != correct:
                item.style = discord.ButtonStyle.danger
            else:
                item.style = discord.ButtonStyle.secondary
            item.disabled = True

    async def on_timeout(self) -> None:
        if self.answered:
            return
        self.answered = True
        correct = self.question["a"]
        self._update_buttons(correct)
        if self.message:
            try:
                await self.message.edit(view=self)
                await self.message.reply(
                    f"⏰ Time's up! The correct answer was "
                    f"**{self.question['c'][correct]}**."
                )
            except Exception:
                pass


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
                send_embeds = [embed]
                if is_villager and nooki_data:
                    house_embed = self.cog.create_villager_house_embed(interaction, display_name, nooki_data)
                    if house_embed:
                        send_embeds.append(house_embed)
                await interaction.response.edit_message(
                    content=f"Hey <@{interaction.user.id}>, look what I found!",
                    embeds=send_embeds,
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
        self.free_island_lookup = {}
        self.channel_topic_cache: dict[int, str] = {}
        self.island_status_since: dict[str, tuple[bool, datetime]] = {}

        # island_clean -> True (down) / False (up); None = not yet initialized
        self.island_down_states: dict[str, bool | None] = {}
        # island_clean -> discord.Message of the sticky "island is down" embed
        self.island_down_messages: dict[str, discord.Message] = {}
        self.island_monitor_loop.start()
        self.topic_sync_loop.start()

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
        """Fetch island channels from Discord sub-category"""
        guild = self.bot.get_guild(Config.GUILD_ID)
        if not guild:
            logger.error(f"[DISCORD] Guild {Config.GUILD_ID} not found.")
            return

        category = discord.utils.get(guild.categories, id=Config.CATEGORY_ID)
        if not category:
            logger.error(f"[DISCORD] Category {Config.CATEGORY_ID} not found.")
            return

        temp_lookup = {}
        fetched_islands = []
        count = 0

        for channel in category.channels:
            if channel.id == Config.IGNORE_CHANNEL_ID:
                continue

            chan_clean = clean_text(channel.name)
            if not chan_clean:
                continue

            # Strip leading digits to get the canonical island name
            # e.g. "01alapaap" -> "alapaap", "bituin" -> "bituin"
            island_clean = re.sub(r'^\d+', '', chan_clean)
            if island_clean:
                temp_lookup[island_clean] = channel.id
                fetched_islands.append(island_clean.title())
                count += 1

        self.sub_island_lookup = temp_lookup

        if fetched_islands:
            Config.SUB_ISLANDS = fetched_islands
            Config.TWITCH_SUB_ISLANDS = fetched_islands

        logger.info(f"[DISCORD] Dynamic Island Fetch Complete. Found {count} islands.")

    async def fetch_free_islands(self):
        """Fetch free island channels from the free-island Discord category."""
        guild = self.bot.get_guild(Config.GUILD_ID)
        if not guild:
            logger.error(f"[DISCORD] Guild {Config.GUILD_ID} not found.")
            return

        if not Config.FREE_CATEGORY_ID:
            logger.warning("[DISCORD] FREE_CATEGORY_ID not configured; free island lookup unavailable.")
            return

        category = discord.utils.get(guild.categories, id=Config.FREE_CATEGORY_ID)
        if not category:
            logger.error(f"[DISCORD] Free island category {Config.FREE_CATEGORY_ID} not found.")
            return

        temp_lookup = {}
        fetched_islands = []
        count = 0

        for channel in category.channels:
            chan_clean = clean_text(channel.name)
            if not chan_clean:
                continue

            # Strip leading digits to get the canonical island name.
            # Some free island channels use a numeric prefix (e.g. "01-kakanggata" → "kakanggata").
            island_clean = re.sub(r'^\d+', '', chan_clean)
            if island_clean:
                temp_lookup[island_clean] = channel.id
                fetched_islands.append(island_clean.title())
                count += 1

        self.free_island_lookup = temp_lookup

        if fetched_islands:
            Config.FREE_ISLANDS = fetched_islands

        logger.info(f"[DISCORD] Dynamic Free Island Fetch Complete. Found {count} islands.")

    def cog_unload(self):
        """Cleanup on unload"""
        self.island_monitor_loop.cancel()
        self.topic_sync_loop.cancel()

    @staticmethod
    def _parse_iso8601(value: str | None) -> datetime | None:
        """Parse an ISO8601 datetime string into a timezone-aware UTC datetime."""
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except Exception:
            return None

    @staticmethod
    def _format_status_duration(since_utc: datetime, now_utc: datetime) -> str:
        """Return elapsed time as '<h>h <m>m' or '<m>m' for topic display."""
        elapsed = max(0, int((now_utc - since_utc).total_seconds()))
        hours, rem = divmod(elapsed, 3600)
        minutes = rem // 60
        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"

    async def _fetch_islands_api_snapshot(self) -> tuple[dict[str, dict], datetime | None] | tuple[None, None]:
        """Fetch island snapshot from the API and return map keyed by cleaned island name."""
        base_url = os.getenv("API_BASE_URL", "https://console.chopaeng.com").rstrip("/")
        url = f"{base_url}/api/islands"

        def _get_json():
            response = requests.get(url, timeout=8)
            response.raise_for_status()
            return response.json()

        try:
            payload = await asyncio.to_thread(_get_json)
        except Exception as exc:
            logger.warning(f"[DISCORD] topic sync API request failed: {exc}")
            return None, None

        data = payload.get("data", []) if isinstance(payload, dict) else []
        island_map: dict[str, dict] = {}
        for item in data:
            name = item.get("name") if isinstance(item, dict) else None
            if not name:
                continue
            normalized = clean_text(name)
            canonical = re.sub(r'^\d+', '', normalized)
            island_map[canonical or normalized] = item

        meta = payload.get("meta", {}) if isinstance(payload, dict) else {}
        api_timestamp = self._parse_iso8601(meta.get("timestamp")) if isinstance(meta, dict) else None
        return island_map, api_timestamp

    def _build_channel_topic(
        self,
        visitor_count: int,
        is_online: bool,
        since_utc: datetime,
        updated_at_utc: datetime,
        now_utc: datetime,
    ) -> str:
        """Build channel topic text from API snapshot data."""
        visitors = max(0, min(7, int(visitor_count)))
        state = "online" if is_online else "offline"
        duration = self._format_status_duration(since_utc, now_utc)
        stamp = updated_at_utc.strftime("%Y-%m-%d %H:%M UTC")
        return f"{visitors}/7 visitors | Island {state} for {duration} | Last update: {stamp}"

    @tasks.loop(seconds=TOPIC_SYNC_INTERVAL_SECONDS)
    async def topic_sync_loop(self):
        """Auto-sync sub island channel topics using /api/islands snapshot data."""
        guild = self.bot.get_guild(Config.GUILD_ID)
        if not guild:
            return

        # Always refresh channel lookup so the loop can recover from stale cache
        # or early startup timing where categories were temporarily unavailable.
        await self.fetch_islands()
        if not self.sub_island_lookup:
            logger.warning("[DISCORD] Topic sync skipped: no sub-island channels discovered yet.")
            return

        island_map, api_timestamp = await self._fetch_islands_api_snapshot()
        if not island_map:
            return

        now_utc = datetime.now(timezone.utc)
        last_update = api_timestamp or now_utc

        updated_count = 0
        missing_payload_count = 0

        for island_clean, channel_id in self.sub_island_lookup.items():
            island_payload = island_map.get(island_clean)
            if not island_payload:
                # Fuzzy fallback for naming drifts between folder name and channel name.
                island_payload = next(
                    (v for k, v in island_map.items() if island_clean in k or k in island_clean),
                    None,
                )
            if not island_payload:
                missing_payload_count += 1
                continue

            channel = guild.get_channel(channel_id)
            if not isinstance(channel, discord.TextChannel):
                continue

            status_text = str(island_payload.get("status", "")).upper()
            bot_online = island_payload.get("discord_bot_online")
            is_online = bool(bot_online) if bot_online is not None else (status_text == "ONLINE")

            state_info = self.island_status_since.get(island_clean)
            if state_info is None or state_info[0] != is_online:
                self.island_status_since[island_clean] = (is_online, now_utc)
                status_since = now_utc
            else:
                status_since = state_info[1]

            visitor_count = island_payload.get("visitors", 0)
            try:
                visitor_count = int(visitor_count)
            except (TypeError, ValueError):
                visitor_count = 0

            topic_text = self._build_channel_topic(
                visitor_count=visitor_count,
                is_online=is_online,
                since_utc=status_since,
                updated_at_utc=last_update,
                now_utc=now_utc,
            )

            if self.channel_topic_cache.get(channel_id) == topic_text:
                continue
            if (channel.topic or "") == topic_text:
                self.channel_topic_cache[channel_id] = topic_text
                continue

            try:
                await channel.edit(topic=topic_text, reason="Auto-sync island status topic from API")
                self.channel_topic_cache[channel_id] = topic_text
                logger.info(f"[DISCORD] Topic synced for #{channel.name}: {topic_text}")
                updated_count += 1
            except discord.Forbidden:
                logger.warning(f"[DISCORD] Missing permission to edit topic for #{channel.name}")
            except discord.HTTPException as exc:
                logger.warning(f"[DISCORD] Failed to edit topic for #{channel.name}: {exc}")

        logger.info(
            f"[DISCORD] Topic sync cycle complete: {updated_count} updated, "
            f"{missing_payload_count} without API match, {len(self.sub_island_lookup)} channels scanned"
        )

    @topic_sync_loop.before_loop
    async def before_topic_sync_loop(self):
        """Wait until ready and ensure island lookups are loaded before topic sync starts."""
        await self.bot.wait_until_ready()
        await self.fetch_islands()

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
            embed = self.create_found_embed(ctx, search_term, found_locations, is_villager=True, nooki_data=nooki_data)

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
                "*Aliases: !locate, !where, !search*"
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
                "`!islands [sub|free]` - Check island bot status (sub, free, or both)\n"
                "*Aliases: !islandstatus, !checkislands*\n"
                "`!trivia` - Play an ACNH quiz question with button answers!\n"
                "*Aliases: !acnhquiz, !quiz*\n"
                "`!status` - Show bot status and cache info\n"
                "`!ping` - Check bot response time\n"
                "`!random` - Get a random item suggestion\n"
                "`!ask <question>` - Ask the Chopaeng AI anything\n"
                "`!help` - Show this help message"
            ),
            inline=False
        )

        embed.add_field(
            name=f"{Config.STAR_PINK} Leaderboard Commands",
            value=(
                "`!topislands [sub|free] [today|week|month|alltime]`\n"
                "↳ Most visited islands. Filter by type and/or time period.\n"
                "*Aliases: !mostvisited*\n"
                "`!toptravellers [sub|free] [today|week|month|alltime]`\n"
                "↳ Top travellers by visit count. Filter by type and/or time period.\n"
                "*Aliases: !toptravelers, !topvisitors*"
            ),
            inline=False
        )
        
        embed.add_field(
            name=f"{Config.STAR_PINK} Flight Logger (Automatic)",
            value=(
                "🛫 Monitors island visitor arrivals in real time\n"
                "🔍 Alerts staff when unknown travelers are detected\n"
                "🛡️ Staff can Admit, Warn, Kick, or Ban via buttons\n"
                "📋 Tracks warnings and moderation history per user\n"
                "`/flight_status` - Diagnose flight logger connection and activity\n"
                "`/recover_flights [hours] [dry/run]` - Recover missing flight records\n"
                "`/unwarn <user>` - Remove all warnings from a user"
            ),
            inline=False
        )

        embed.add_field(
            name=f"{Config.STAR_PINK} Island Alert Subscriptions",
            value=(
                "`!subscribe <island>` - Get a DM when an island comes online/offline\n"
                "*Aliases: !islandalert*\n"
                "`!unsubscribe <island|all>` - Remove an alert (or all alerts)\n"
                "*Aliases: !unislandalert*\n"
                "`!mysubscriptions` - List your active island alert subscriptions\n"
                "*Aliases: !mysubs, !myalerts*"
            ),
            inline=False
        )

        embed.add_field(
            name=f"{Config.STAR_PINK} Admin Commands",
            value=(
                "`!refresh` - Manually refresh cache (Admin only)\n"
                "`!update` - Pull latest code from git and restart the bot (Admin only)"
            ),
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

    async def _enforce_find_channel(self, ctx) -> bool:
        """
        Returns True if in the correct channel.
        Otherwise, deletes the message and returns False.
        """
        if not Config.FIND_BOT_CHANNEL_ID or ctx.channel.id == Config.FIND_BOT_CHANNEL_ID:
            return True

        # Nuke the unauthorized text command
        if ctx.message:
            try:
                await ctx.message.delete()
            except discord.HTTPException:
                pass

        # Scold them ephemerally if they used a slash command
        if ctx.interaction and not ctx.interaction.response.is_done():
            try:
                await ctx.interaction.response.send_message(
                    f"Keep it clean. Use this command in <#{Config.FIND_BOT_CHANNEL_ID}>.",
                    ephemeral=True
                )
            except discord.HTTPException:
                pass

        return False

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

    @commands.hybrid_command(name="trivia", aliases=["acnhquiz", "quiz"])
    async def trivia(self, ctx):
        """Play an ACNH trivia question! Answer with the buttons before time runs out."""
        q = random.choice(ACNH_TRIVIA_QUESTIONS)
        letter = _TRIVIA_LETTER
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
        )

        await ctx.reply(f"{answer}")
        logger.info(f"[DISCORD] Ask command by {ctx.author.name}: {question[:80]}")

    @commands.hybrid_command(name="islands", aliases=["islandstatus", "checkislands"])
    @app_commands.describe(kind="Which islands to check: sub, free, or leave blank for both.")
    @app_commands.choices(kind=[
        app_commands.Choice(name="sub — Sub Islands",   value="sub"),
        app_commands.Choice(name="free — Free Islands", value="free"),
    ])
    async def island_status(self, ctx, kind: str = ""):
        """Check island bot status. Use 'sub', 'free', or leave blank for both."""
        await ctx.defer()

        guild = self.bot.get_guild(Config.GUILD_ID)
        if not guild:
            await ctx.reply("Guild not found.")
            return

        kind = kind.strip().lower()

        # Validate explicit arguments
        if kind and kind not in ("sub", "free"):
            await ctx.reply("Usage: `/islands [sub|free]`", ephemeral=True)
            return

        show_sub  = kind in ("", "sub")
        show_free = kind in ("", "free")

        island_bot_role = guild.get_role(Config.ISLAND_BOT_ROLE_ID) if Config.ISLAND_BOT_ROLE_ID else None
        if Config.ISLAND_BOT_ROLE_ID and not island_bot_role:
            logger.warning(f"[DISCORD] ISLAND_BOT_ROLE_ID {Config.ISLAND_BOT_ROLE_ID} not found in guild; bot name matching disabled")

        # --- Sub island results ---
        sub_results: list = []
        sub_online = 0
        if show_sub:
            await self.fetch_islands()
            for island in Config.SUB_ISLANDS:
                island_clean = clean_text(island)
                channel_id = self.sub_island_lookup.get(island_clean)

                if not channel_id:
                    for ch in guild.channels:
                        if isinstance(ch, discord.TextChannel) and island_clean in clean_text(ch.name):
                            channel_id = ch.id
                            break

                if not channel_id:
                    sub_results.append((island, "❓", "Channel not found", None))
                    continue

                channel = guild.get_channel(channel_id)
                if not channel:
                    sub_results.append((island, "❓", "Channel not found", None))
                    continue

                island_bot = None
                if island_bot_role:
                    target = clean_text(f"chobot {island}")
                    for member in island_bot_role.members:
                        if member.bot and clean_text(member.display_name) == target:
                            island_bot = member
                            break

                if island_bot and island_bot.status in (discord.Status.online, discord.Status.idle):
                    sub_results.append((island, "✅", "Bot online", channel_id))
                    sub_online += 1
                    continue

                try:
                    messages = [msg async for msg in channel.history(limit=25)]
                except discord.Forbidden:
                    sub_results.append((island, "❓", "No channel access", channel_id))
                    continue

                island_up   = False
                status_reason = ""
                for msg in messages:
                    if island_bot:
                        if msg.author.id != island_bot.id:
                            continue
                    elif not msg.author.bot:
                        continue
                    if DODO_CODE_PATTERN.search(msg.content):
                        island_up = True
                        status_reason = "Dodo code active"
                        break
                    if ISLAND_HOST_NAME in msg.content.lower():
                        island_up = True
                        status_reason = "Chopaeng is visiting"
                        break

                if island_up:
                    sub_results.append((island, "✅", status_reason, channel_id))
                    sub_online += 1
                else:
                    sub_results.append((island, "❌", "No recent activity", channel_id))

        # --- Free island results ---
        free_results: list = []
        free_online = 0
        if show_free:
            await self.fetch_free_islands()
            for island in Config.FREE_ISLANDS:
                island_clean = clean_text(island)
                channel_id = self.free_island_lookup.get(island_clean)

                island_bot = None
                if island_bot_role:
                    target = clean_text(f"chobot {island}")
                    for member in island_bot_role.members:
                        if member.bot and clean_text(member.display_name) == target:
                            island_bot = member
                            break

                if island_bot and island_bot.status in (discord.Status.online, discord.Status.idle):
                    free_results.append((island, "✅", "Bot online", channel_id))
                    free_online += 1
                elif island_bot:
                    free_results.append((island, "❌", "Bot offline", channel_id))
                else:
                    free_results.append((island, "❓", "Bot not found", channel_id))

        # --- Build embed(s) ---
        pfp_url = ctx.author.avatar.url if ctx.author.avatar else Config.DEFAULT_PFP

        if kind == "sub":
            total = len(Config.SUB_ISLANDS)
            embed = discord.Embed(
                title="🏝️ Sub Island Status",
                description=f"**{sub_online}/{total}** islands active",
                color=discord.Color.green() if sub_online == total else (
                    discord.Color.orange() if sub_online > 0 else discord.Color.red()
                ),
                timestamp=discord.utils.utcnow()
            )
            on_lines  = [f"<#{c}>" if c else f"**{n}**" for n, s, _, c in sub_results if s == "✅"]
            off_lines = [f"<#{c}>" if c else f"**{n}**" for n, s, _, c in sub_results if s != "✅"]
            embed.add_field(name="🟢 ONLINE",  value="\n".join(on_lines)  or "*none*", inline=True)
            embed.add_field(name="🔴 OFFLINE", value="\n".join(off_lines) or "*none*", inline=True)
            embed.set_footer(text=f"Requested by {ctx.author.display_name}", icon_url=pfp_url)
            embed.set_image(url=Config.FOOTER_LINE)
            await ctx.reply(embed=embed)
            logger.info(f"[DISCORD] Sub island status check: {sub_online}/{total} online")

        elif kind == "free":
            total = len(Config.FREE_ISLANDS)
            embed = discord.Embed(
                title="🌴 Free Island Status",
                description=f"**{free_online}/{total}** islands active",
                color=discord.Color.green() if free_online == total else (
                    discord.Color.orange() if free_online > 0 else discord.Color.red()
                ),
                timestamp=discord.utils.utcnow()
            )
            on_lines  = [f"<#{c}>" if c else f"**{n}**" for n, s, _, c in free_results if s == "✅"]
            off_lines = [f"<#{c}>" if c else f"**{n}**" for n, s, _, c in free_results if s != "✅"]
            embed.add_field(name="🟢 ONLINE",  value="\n".join(on_lines)  or "*none*", inline=True)
            embed.add_field(name="🔴 OFFLINE", value="\n".join(off_lines) or "*none*", inline=True)
            embed.set_footer(text=f"Requested by {ctx.author.display_name}", icon_url=pfp_url)
            embed.set_image(url=Config.FOOTER_LINE)
            await ctx.reply(embed=embed)
            logger.info(f"[DISCORD] Free island status check: {free_online}/{total} online")

        else:
            # Combined embed (no argument)
            sub_total  = len(Config.SUB_ISLANDS)
            free_total = len(Config.FREE_ISLANDS)
            total      = sub_total + free_total
            combined_online = sub_online + free_online
            embed = discord.Embed(
                title="🏝️ Island Status",
                description=f"**{combined_online}/{total}** islands active",
                color=discord.Color.green() if combined_online == total else (
                    discord.Color.orange() if combined_online > 0 else discord.Color.red()
                ),
                timestamp=discord.utils.utcnow()
            )
            sub_on  = [f"<#{c}>" if c else f"**{n}**" for n, s, _, c in sub_results  if s == "✅"]
            sub_off = [f"<#{c}>" if c else f"**{n}**" for n, s, _, c in sub_results  if s != "✅"]
            free_on  = [f"<#{c}>" if c else f"**{n}**" for n, s, _, c in free_results if s == "✅"]
            free_off = [f"<#{c}>" if c else f"**{n}**" for n, s, _, c in free_results if s != "✅"]
            embed.add_field(
                name=f"🏝️ Sub — {sub_online}/{sub_total}",
                value=(
                    ("🟢 " + "\n🟢 ".join(sub_on)  if sub_on  else "") +
                    ("\n" if sub_on and sub_off else "") +
                    ("🔴 " + "\n🔴 ".join(sub_off) if sub_off else "") or "*none*"
                ),
                inline=True,
            )
            embed.add_field(
                name=f"🌴 Free — {free_online}/{free_total}",
                value=(
                    ("🟢 " + "\n🟢 ".join(free_on)  if free_on  else "") +
                    ("\n" if free_on and free_off else "") +
                    ("🔴 " + "\n🔴 ".join(free_off) if free_off else "") or "*none*"
                ),
                inline=True,
            )
            embed.set_footer(text=f"Requested by {ctx.author.display_name}", icon_url=pfp_url)
            embed.set_image(url=Config.FOOTER_LINE)
            await ctx.reply(embed=embed)
            logger.info(f"[DISCORD] Combined island status: sub {sub_online}/{sub_total}, free {free_online}/{free_total}")

    # Backward-compatible aliases so !freeislands / !freeislandstatus still work
    @commands.command(name="freeislands", aliases=["freeislandstatus", "checkfreeislands"], hidden=True)
    async def free_island_status(self, ctx):
        """Alias for !islands free"""
        await self.island_status(ctx, kind="free")

    async def _check_sub_island_status(self, ctx) -> bool:
        """Check if the sub island bot is online. Replies with a down embed and returns False if not."""
        if not self._is_sub_island_channel(ctx.channel):
            await ctx.reply("This command can only be used in a sub island channel.", ephemeral=True)
            return False

        if self.check_cooldown(str(ctx.author.id)):
            return False

        guild = self.bot.get_guild(Config.GUILD_ID)
        island_bot = self._get_island_bot_for_channel(guild, ctx.channel) if guild else None

        if island_bot and island_bot.status in (discord.Status.online, discord.Status.idle):
            logger.info(f"[DISCORD] Island bot online for {ctx.channel.name}, doing nothing")
            return True

        await ctx.reply(embed=self._create_island_down_embed(ctx))
        logger.info(f"[DISCORD] Island bot offline for {ctx.channel.name}")
        return False

    @commands.hybrid_command(name="senddodo", aliases=["sd"])
    async def send_dodo(self, ctx):
        """Send the dodo code to a user via DM"""
        if not self._is_sub_island_channel(ctx.channel):
            await ctx.reply("This command can only be used in a sub island channel. Please read the sticky post below carefully and make sure you understand and follow all the <#783677194576330792> before agreeing to them.", ephemeral=True)
            return

        if self.check_cooldown(str(ctx.author.id)):
            return

        guild = self.bot.get_guild(Config.GUILD_ID)
        island_bot = self._get_island_bot_for_channel(guild, ctx.channel) if guild else None

        if not island_bot or island_bot.status not in (discord.Status.online, discord.Status.idle):
            await ctx.reply(embed=self._create_island_down_embed(ctx))
            return

        def dodo_check(msg):
            return (
                msg.author.id == island_bot.id
                and msg.channel.id == ctx.channel.id
                and ISLAND_DODO_SENT_PATTERN.search(msg.content)
            )

        try:
            island_msg = await self.bot.wait_for('message', check=dodo_check, timeout=ISLAND_BOT_INTERCEPT_TIMEOUT)
            await island_msg.delete()
            reply_msg = await ctx.reply(embed=self._build_dodo_sent_embed(ctx))
            logger.info(f"[DISCORD] Intercepted and redesigned !sd response for {ctx.channel.name}")
            await self._log_dodo_request_to_xlog(ctx, reply_msg)
        except asyncio.TimeoutError:
            logger.warning(f"[DISCORD] Timeout waiting for island bot !sd response in {ctx.channel.name}")
            await ctx.reply(embed=self._create_island_down_embed(ctx))

    @commands.hybrid_command(name="visitors")
    async def visitors(self, ctx):
        """Check current visitors on the sub island"""
        if not self._is_sub_island_channel(ctx.channel):
            await ctx.reply("This command can only be used in a sub island channel.", ephemeral=True)
            return

        if self.check_cooldown(str(ctx.author.id)):
            return

        guild = self.bot.get_guild(Config.GUILD_ID)
        island_bot = self._get_island_bot_for_channel(guild, ctx.channel) if guild else None

        if not island_bot or island_bot.status not in (discord.Status.online, discord.Status.idle):
            await ctx.reply(embed=self._create_island_down_embed(ctx))
            return

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
            await ctx.reply(embed=self._create_island_down_embed(ctx))

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

    def _build_visitors_embed(self, ctx, island_name: str, visitor_lines: list) -> discord.Embed:
        """Build a nicely formatted visitors embed from a parsed visitor list."""
        filled = [v for v in visitor_lines if v.lower() != AVAILABLE_SLOT_TEXT]
        total = len(visitor_lines)
        available = total - len(filled)

        visitor_display = []
        for i, v in enumerate(visitor_lines, 1):
            if v.lower() == AVAILABLE_SLOT_TEXT:
                visitor_display.append(f"`#{i}` 〰️ *Available*")
            else:
                visitor_display.append(f"`#{i}` 🧑‍🤝‍🧑 **{v}**")

        color = discord.Color.green() if available > 0 else discord.Color.red()
        embed = discord.Embed(
            title=f"🏝Visitors on {island_name}",
            description="\n".join(visitor_display) if visitor_display else "*No visitor data available.*",
            color=color,
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(
            name="Slots",
            value=f"`{len(filled)}/{total}` occupied · `{available}` available",
            inline=False
        )
        pfp_url = ctx.author.avatar.url if ctx.author.avatar else Config.DEFAULT_PFP
        embed.set_footer(text=f"Requested by {ctx.author.display_name}", icon_url=pfp_url)
        embed.set_image(url=Config.FOOTER_LINE)
        return embed

    def _build_dodo_sent_embed(self, ctx) -> discord.Embed:
        """Build a nicely formatted 'dodo code sent' embed."""
        embed = discord.Embed(
            title="✈️ Dodo Code Sent!",
            description=(
                f"Hey {ctx.author.mention}! The dodo code has been sent to your DMs.\n\n"
                "Head to the airport and open the **Dodo Airlines** app to enter it!"
            ),
            color=discord.Color.blue(),
            timestamp=discord.utils.utcnow()
        )
        pfp_url = ctx.author.avatar.url if ctx.author.avatar else Config.DEFAULT_PFP
        embed.set_footer(text=f"Requested by {ctx.author.display_name}", icon_url=pfp_url)
        embed.set_image(url=Config.FOOTER_LINE)
        return embed

    async def _log_dodo_request_to_xlog(self, ctx, reply_msg: discord.Message | None) -> None:
        """Post a notification to the xlog channel when a user successfully requests the dodo code.

        If the flight logger is available, defers the xlog post until the user is seen joining
        (or DODO_XLOG_TIMEOUT seconds elapse, whichever comes first).
        """
        xlog_channel = self.bot.get_channel(Config.XLOG_VERBOSE_CHANNEL_ID)
        if not xlog_channel:
            return

        guild = self.bot.get_guild(Config.GUILD_ID)
        guild_icon = guild.icon.url if guild and guild.icon else None

        flight_cog = self.bot.get_cog('FlightLoggerCog')
        if flight_cog:
            flight_cog.register_dodo_request(ctx.author.id, ctx.author, ctx.channel, reply_msg, guild_icon)

            async def _guarded_fallback():
                try:
                    await self._post_dodo_xlog_fallback(ctx, reply_msg, guild_icon, xlog_channel)
                except Exception as e:
                    logger.warning(f"[DISCORD] Dodo xlog fallback task failed: {e}")

            asyncio.create_task(_guarded_fallback())
            return

        await self._send_dodo_request_xlog(ctx, reply_msg, guild_icon, xlog_channel)

    async def _post_dodo_xlog_fallback(self, ctx, reply_msg: discord.Message | None, guild_icon: str | None, xlog_channel) -> None:
        """After DODO_XLOG_TIMEOUT, post the dodo-request xlog if the flight logger hasn't already merged it."""
        await asyncio.sleep(DODO_XLOG_TIMEOUT)
        flight_cog = self.bot.get_cog('FlightLoggerCog')
        if flight_cog:
            pending = flight_cog.pop_pending_dodo_request(ctx.author.id)
            if pending is None:
                return  # Already merged into the verified-flight xlog entry

        visit_id = None
        try:
            guild = self.bot.get_guild(Config.GUILD_ID)
            if flight_cog and guild:
                visit_id = await flight_cog.get_recent_visit_id_by_user(ctx.author.id, guild.id)
        except Exception as e:
            logger.warning(f"[DISCORD] Could not look up visit ID for xlog fallback: {e}")

        await self._send_dodo_request_xlog(ctx, reply_msg, guild_icon, xlog_channel, visit_id=visit_id)

    async def _send_dodo_request_xlog(self, ctx, reply_msg: discord.Message | None, guild_icon: str | None, xlog_channel, visit_id: int | None = None) -> None:
        """Build and send the dodo-request embed to xlog."""
        embed = discord.Embed(
            title="✈️ Dodo Code Requested",
            description=(
                f"{ctx.author.mention} requested the dodo code in {ctx.channel.mention}."
            ),
            color=discord.Color.blue(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Member",  value=f"{ctx.author.mention} ({ctx.author.display_name})", inline=True)
        embed.add_field(name="Channel", value=ctx.channel.mention,                                  inline=True)
        if visit_id is not None:
            embed.add_field(name="Visit ID", value=f"`#{visit_id}`",                                inline=True)
        embed.set_image(url=Config.FOOTER_LINE)
        embed.set_footer(text="Chopaeng Camp™ • Dodo Request", icon_url=guild_icon)

        view = discord.ui.View()
        if reply_msg:
            view.add_item(discord.ui.Button(label="View Request", url=reply_msg.jump_url, style=discord.ButtonStyle.link))

        try:
            await xlog_channel.send(embed=embed, view=view)
        except Exception as e:
            logger.warning(f"[DISCORD] Failed to post dodo request to xlog: {e}")

    async def _check_island_online(self, guild: discord.Guild, island: str, lookup: dict | None = None) -> bool:
        """Return True if the island appears to be online, False otherwise.

        ``lookup`` should be the channel-name → channel-id mapping for the island
        type being checked (sub or free).  Keys must be normalised with
        ``clean_text()`` — the same normalisation applied when the lookup was
        built.  Defaults to ``self.sub_island_lookup``.
        """
        island_clean = clean_text(island)
        effective_lookup = lookup if lookup is not None else self.sub_island_lookup
        channel_id = effective_lookup.get(island_clean)
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

    async def _notify_island_subscribers(self, island_clean: str, island_display: str, online: bool) -> None:
        """DM all subscribers for *island_clean* about a status change.

        *online* is True when the island just came back up, False when it went down.
        Failed DMs (e.g. DMs disabled) are silently skipped.
        """
        user_ids = _get_island_subscribers(island_clean)
        if not user_ids:
            return

        if online:
            title = "🏝️ Island is Back Up!"
            description = (
                f"**{island_display.title()}** island is back online and ready to visit! 🎉\n"
                f"Head to the island channel and use `!senddodo` or `!sd` to get the Dodo code."
            )
            color = discord.Color.green()
        else:
            title = "🏝️ Island is Down"
            description = (
                f"**{island_display.title()}** island has gone **offline**.\n"
                f"You'll be notified again when it comes back up."
            )
            color = discord.Color.red()

        embed = discord.Embed(
            title=title,
            description=description,
            color=color,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(text="Use !unsubscribe to stop these alerts.")

        sent = 0
        for uid in user_ids:
            try:
                user = self.bot.get_user(uid) or await self.bot.fetch_user(uid)
                await user.send(embed=embed)
                sent += 1
            except (discord.Forbidden, discord.NotFound):
                pass
            except Exception as exc:
                logger.warning(f"[DISCORD] Could not DM subscriber {uid} for {island_clean}: {exc}")

        if sent:
            logger.info(f"[DISCORD] Notified {sent} subscriber(s) that {island_display} is {'back ONLINE' if online else 'OFFLINE'}")

    @tasks.loop(seconds=30)
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

            # Persist current status to the database so the REST API can expose it
            _upsert_bot_status(island.lower(), island, is_online)

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
                    msg = await channel.send(embed=embed)
                    self.island_down_messages[island_clean] = msg
                    logger.info(f"[DISCORD] Island monitor: {island} went OFFLINE")
                except Exception as e:
                    logger.error(f"[DISCORD] Failed to send island-down embed for {island}: {e}")

                # DM subscribers about the outage
                await self._notify_island_subscribers(island_clean, island, online=False)

            elif is_online and was_down:
                # Transition: offline → online
                self.island_down_states[island_clean] = False
                # Remove the sticky "island is down" embed
                sticky_msg = self.island_down_messages.pop(island_clean, None)
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
        if self.free_island_lookup:
            for island in Config.FREE_ISLANDS:
                free_island_clean = clean_text(island)
                try:
                    is_online = await self._check_island_online(guild, island, lookup=self.free_island_lookup)
                except Exception as e:
                    logger.error(f"[DISCORD] island_monitor_loop error checking free island {island}: {e}")
                    continue
                _upsert_bot_status(island.lower(), island, is_online)

                # Track transitions for free islands so subscribers can be notified
                free_was_down = self.island_down_states.get(f"free:{free_island_clean}")
                if free_was_down is None:
                    self.island_down_states[f"free:{free_island_clean}"] = False
                    continue
                if not is_online and not free_was_down:
                    self.island_down_states[f"free:{free_island_clean}"] = True
                    await self._notify_island_subscribers(free_island_clean, island, online=False)
                elif is_online and free_was_down:
                    self.island_down_states[f"free:{free_island_clean}"] = False
                    await self._notify_island_subscribers(free_island_clean, island, online=True)

    @island_monitor_loop.before_loop
    async def before_island_monitor_loop(self):
        """Wait until bot is ready before starting the island monitor."""
        await self.bot.wait_until_ready()
        await self.fetch_islands()
        await self.fetch_free_islands()

    # ── Period choices shared by both leaderboard commands ──────────────────
    _PERIOD_LABELS = {
        "today":   "Today",
        "week":    "Last 7 Days",
        "month":   "This Month",
        "alltime": "All Time",
        "":        "All Time",
    }

    @staticmethod
    def _period_cutoff(period: str) -> int | None:
        """Return a Unix-timestamp lower-bound for the given period, or None for all-time.

        Timestamps in island_visits are stored as UTC Unix seconds.  The server
        is treated as UTC+8 for day/month boundaries (matching the dashboard).
        """
        TZ8 = timezone(timedelta(hours=8))
        now8 = datetime.now(TZ8)
        period = period.lower().strip()
        if period == "today":
            midnight = now8.replace(hour=0, minute=0, second=0, microsecond=0)
            return int(midnight.astimezone(timezone.utc).timestamp())
        if period == "week":
            delta = now8 - timedelta(days=7)
            return int(delta.astimezone(timezone.utc).timestamp())
        if period == "month":
            first = now8.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            return int(first.astimezone(timezone.utc).timestamp())
        return None  # alltime / ""

    @commands.hybrid_command(name="topislands", aliases=["mostvisited"])
    @app_commands.describe(
        kind="Filter by island type: 'sub', 'free', or leave blank for both.",
        period="Time period: today, week, month, or alltime (default).",
    )
    @app_commands.choices(
        kind=[
            app_commands.Choice(name="sub — Sub Islands",   value="sub"),
            app_commands.Choice(name="free — Free Islands", value="free"),
        ],
        period=[
            app_commands.Choice(name="Today",        value="today"),
            app_commands.Choice(name="Last 7 Days",  value="week"),
            app_commands.Choice(name="This Month",   value="month"),
            app_commands.Choice(name="All Time",     value="alltime"),
        ],
    )
    async def top_islands(self, ctx, kind: str = "", period: str = "alltime"):
        """Show the most visited islands. Filter by island type and/or time period."""
        kind   = kind.lower().strip()
        period = period.lower().strip()
        if kind not in ("sub", "free", ""):
            await ctx.reply("Please use `sub`, `free`, or leave blank for both.", ephemeral=True)
            return
        if period not in ("today", "week", "month", "alltime", ""):
            await ctx.reply("Please use `today`, `week`, `month`, or `alltime`.", ephemeral=True)
            return

        cutoff = self._period_cutoff(period)

        try:
            loop = asyncio.get_event_loop()

            def _query():
                with sqlite3.connect(_DB_PATH, timeout=5) as conn:
                    conn.row_factory = sqlite3.Row
                    clauses, params = [], []
                    if kind:
                        clauses.append("island_type = ?")
                        params.append(kind)
                    if cutoff is not None:
                        clauses.append("timestamp >= ?")
                        params.append(cutoff)
                    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
                    rows = conn.execute(
                        f"SELECT destination, COUNT(*) AS visit_count "
                        f"FROM island_visits {where} "
                        f"GROUP BY destination ORDER BY visit_count DESC LIMIT 10",
                        params,
                    ).fetchall()
                    return [dict(r) for r in rows]

            rows = await loop.run_in_executor(None, _query)
        except Exception as exc:
            logger.error(f"[DISCORD] topislands DB error: {exc}")
            await ctx.reply("Could not retrieve island data right now. Please try again later.", ephemeral=True)
            return

        kind_label   = {"sub": "Sub Islands", "free": "Free Islands", "": "All Islands"}[kind]
        period_label = self._PERIOD_LABELS.get(period, "All Time")
        title = f"Most Visited Islands — {kind_label} · {period_label}"
        pfp_url = ctx.author.avatar.url if ctx.author.avatar else Config.DEFAULT_PFP

        if not rows:
            embed = discord.Embed(
                title=title,
                description="No visit data found for this period.",
                color=discord.Color.blurple(),
                timestamp=discord.utils.utcnow(),
            )
            embed.set_footer(text=f"Requested by {ctx.author.display_name}", icon_url=pfp_url)
            embed.set_image(url=Config.FOOTER_LINE)
            await ctx.reply(embed=embed)
            return

        lines = []
        for i, row in enumerate(rows):
            lines.append(
                f"{Config.STAR_PINK} `#{i + 1}` **{row['destination']}** — `{row['visit_count']:,}` visits"
            )

        embed = discord.Embed(
            title=title,
            description="\n".join(lines),
            color=discord.Color.gold(),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(text=f"Requested by {ctx.author.display_name}", icon_url=pfp_url)
        embed.set_image(url=Config.FOOTER_LINE)
        await ctx.reply(embed=embed)
        logger.info(f"[DISCORD] topislands called by {ctx.author.name} (kind={kind!r}, period={period!r})")

    @commands.hybrid_command(name="toptravellers", aliases=["toptravelers", "topvisitors"])
    @app_commands.describe(
        kind="Filter by island type: 'sub', 'free', or leave blank for both.",
        period="Time period: today, week, month, or alltime (default).",
    )
    @app_commands.choices(
        kind=[
            app_commands.Choice(name="sub — Sub Islands",   value="sub"),
            app_commands.Choice(name="free — Free Islands", value="free"),
        ],
        period=[
            app_commands.Choice(name="Today",        value="today"),
            app_commands.Choice(name="Last 7 Days",  value="week"),
            app_commands.Choice(name="This Month",   value="month"),
            app_commands.Choice(name="All Time",     value="alltime"),
        ],
    )
    async def top_travellers(self, ctx, kind: str = "", period: str = "alltime"):
        """Show the top travellers by visit count. Filter by island type and/or time period."""
        kind   = kind.lower().strip()
        period = period.lower().strip()
        if kind not in ("sub", "free", ""):
            await ctx.reply("Please use `sub`, `free`, or leave blank for both.", ephemeral=True)
            return
        if period not in ("today", "week", "month", "alltime", ""):
            await ctx.reply("Please use `today`, `week`, `month`, or `alltime`.", ephemeral=True)
            return

        cutoff = self._period_cutoff(period)

        try:
            loop = asyncio.get_event_loop()

            def _query():
                with sqlite3.connect(_DB_PATH, timeout=5) as conn:
                    conn.row_factory = sqlite3.Row
                    clauses, params = [], []
                    if kind:
                        clauses.append("island_type = ?")
                        params.append(kind)
                    if cutoff is not None:
                        clauses.append("timestamp >= ?")
                        params.append(cutoff)
                    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
                    rows = conn.execute(
                        f"SELECT ign, COUNT(*) AS visit_count "
                        f"FROM island_visits {where} "
                        f"GROUP BY ign ORDER BY visit_count DESC LIMIT 10",
                        params,
                    ).fetchall()
                    return [dict(r) for r in rows]

            rows = await loop.run_in_executor(None, _query)
        except Exception as exc:
            logger.error(f"[DISCORD] toptravellers DB error: {exc}")
            await ctx.reply("Could not retrieve traveller data right now. Please try again later.", ephemeral=True)
            return

        kind_label   = {"sub": "Sub Islands", "free": "Free Islands", "": "All Islands"}[kind]
        period_label = self._PERIOD_LABELS.get(period, "All Time")
        title = f"Top Travellers — {kind_label} · {period_label}"
        pfp_url = ctx.author.avatar.url if ctx.author.avatar else Config.DEFAULT_PFP

        if not rows:
            embed = discord.Embed(
                title=title,
                description="No traveller data found for this period.",
                color=discord.Color.blurple(),
                timestamp=discord.utils.utcnow(),
            )
            embed.set_footer(text=f"Requested by {ctx.author.display_name}", icon_url=pfp_url)
            embed.set_image(url=Config.FOOTER_LINE)
            await ctx.reply(embed=embed)
            return

        lines = []
        for i, row in enumerate(rows):
            lines.append(
                f"{Config.STAR_PINK} `#{i + 1}` **{row['ign']}** — `{row['visit_count']:,}` visits"
            )

        embed = discord.Embed(
            title=title,
            description="\n".join(lines),
            color=discord.Color.purple(),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(text=f"Requested by {ctx.author.display_name}", icon_url=pfp_url)
        embed.set_image(url=Config.FOOTER_LINE)
        await ctx.reply(embed=embed)
        logger.info(f"[DISCORD] toptravellers called by {ctx.author.name} (kind={kind!r}, period={period!r})")

    # ── Island subscription autocomplete ────────────────────────────────────

    async def island_name_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete helper: combines sub + free island names."""
        all_islands = sorted(
            set(self.sub_island_lookup.keys()) | set(self.free_island_lookup.keys())
        )
        current_lower = current.lower()
        matches = [n for n in all_islands if current_lower in n] if current else all_islands
        return [
            app_commands.Choice(name=name.title(), value=name)
            for name in matches[:25]
        ]

    # ── Subscription commands ─────────────────────────────────────────────

    @commands.hybrid_command(name="subscribe", aliases=["islandalert"])
    @app_commands.describe(island="The island you want to be notified about when it comes online")
    @app_commands.autocomplete(island=island_name_autocomplete)
    async def subscribe_island(self, ctx, *, island: str = ""):
        """Subscribe to DM alerts when an island comes back online."""
        if not island:
            await ctx.reply(
                "Usage: `!subscribe <island>` — e.g. `!subscribe alapaap`\n"
                "You'll receive a DM when that island comes back online.",
                ephemeral=True,
            )
            return

        island_clean = clean_text(island)
        if not island_clean:
            await ctx.reply("Please provide a valid island name.", ephemeral=True)
            return

        # Determine island kind
        if island_clean in self.sub_island_lookup:
            kind = "sub"
        elif island_clean in self.free_island_lookup:
            kind = "free"
        else:
            # Suggest closest match
            all_islands = sorted(
                set(self.sub_island_lookup.keys()) | set(self.free_island_lookup.keys())
            )
            suggestion = ""
            if all_islands:
                best = process.extractOne(island_clean, all_islands, scorer=fuzz.ratio)
                if best and best[1] >= 60:
                    suggestion = f" Did you mean **{best[0].title()}**?"
            await ctx.reply(
                f"Island **{island.title()}** not found.{suggestion}",
                ephemeral=True,
            )
            return

        added = _add_subscription(ctx.author.id, island_clean, kind)
        if added:
            await ctx.reply(
                f"✅ You'll be DM'd when **{island_clean.title()}** comes back online!",
                ephemeral=True,
            )
            logger.info(f"[DISCORD] {ctx.author.name} subscribed to {island_clean} ({kind})")
        else:
            await ctx.reply(
                f"You're already subscribed to **{island_clean.title()}** alerts.",
                ephemeral=True,
            )

    @commands.hybrid_command(name="unsubscribe", aliases=["unislandalert"])
    @app_commands.describe(island="Island to stop alerts for, or 'all' to remove all subscriptions")
    @app_commands.autocomplete(island=island_name_autocomplete)
    async def unsubscribe_island(self, ctx, *, island: str = ""):
        """Unsubscribe from island online alerts."""
        if not island:
            await ctx.reply(
                "Usage: `!unsubscribe <island>` or `!unsubscribe all`",
                ephemeral=True,
            )
            return

        if island.strip().lower() == "all":
            removed = _remove_subscription(ctx.author.id, None)
            if removed:
                await ctx.reply("✅ Removed all your island alert subscriptions.", ephemeral=True)
            else:
                await ctx.reply("You have no active island alert subscriptions.", ephemeral=True)
            logger.info(f"[DISCORD] {ctx.author.name} unsubscribed from all islands")
            return

        island_clean = clean_text(island)
        removed = _remove_subscription(ctx.author.id, island_clean)
        if removed:
            await ctx.reply(
                f"✅ You'll no longer receive alerts for **{island_clean.title()}**.",
                ephemeral=True,
            )
            logger.info(f"[DISCORD] {ctx.author.name} unsubscribed from {island_clean}")
        else:
            await ctx.reply(
                f"You weren't subscribed to **{island_clean.title()}** alerts.",
                ephemeral=True,
            )

    @commands.hybrid_command(name="mysubscriptions", aliases=["mysubs", "myalerts"])
    async def my_subscriptions(self, ctx):
        """List all your active island alert subscriptions."""
        subs = _get_user_subscriptions(ctx.author.id)
        if not subs:
            await ctx.reply(
                "You have no active island alert subscriptions.\n"
                "Use `!subscribe <island>` to get DM'd when an island comes back online.",
                ephemeral=True,
            )
            return

        lines = [f"• **{name.title()}** ({kind})" for name, kind in subs]
        embed = discord.Embed(
            title="🔔 Your Island Alert Subscriptions",
            description="\n".join(lines),
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(
            text="Use !unsubscribe <island> or !unsubscribe all to cancel.",
            icon_url=ctx.author.avatar.url if ctx.author.avatar else Config.DEFAULT_PFP,
        )
        await ctx.reply(embed=embed, ephemeral=True)
        logger.info(f"[DISCORD] {ctx.author.name} checked their subscriptions ({len(subs)} total)")

    @commands.hybrid_command(name="refresh")
    @commands.has_permissions(administrator=True)
    async def refresh(self, ctx):
        """Manually refresh cache (Mods only)"""
        await ctx.reply("Refreshing cache and island links...")
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.data_manager.update_cache)
        await self.fetch_islands()
        await self.fetch_free_islands()
        count = len(getattr(self, 'island_map', {})) 
        await ctx.reply(f"Done. Linked {count} islands.")

    @refresh.error
    async def refresh_error(self, ctx, error):
        """Handle permission errors cleanly"""
        if isinstance(error, commands.MissingPermissions):
            await ctx.reply("You do not have permission to use this command.")

    @commands.hybrid_command(name="update")
    @commands.has_permissions(administrator=True)
    async def update(self, ctx):
        """OTA update: pull latest code from git and restart the bot (Admin only)"""
        await ctx.reply("Fetching latest changes from git...")

        # Run git pull, forcing English output for reliable message parsing
        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    ['git', 'pull'],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    env={**os.environ, 'LANG': 'C', 'LC_ALL': 'C'},
                )
            )
            git_output = result.stdout.strip() or result.stderr.strip() or "No output."
        except Exception as e:
            await ctx.reply(f"Git pull failed: `{e}`")
            return

        await ctx.reply(f"```\n{git_output[:GIT_OUTPUT_MAX_LENGTH]}\n```")

        if result.returncode != 0:
            await ctx.reply(
                f"Git pull failed (exit code {result.returncode}). Not restarting."
            )
            return

        if "already up to date" in git_output.lower():
            await ctx.reply("Already up to date. No restart needed.")
            return

        await ctx.reply("Update pulled! Restarting bot now...")
        logger.info("[DISCORD] OTA update pulled new code. Restarting process...")

        # Signal main() to call os.execv() from the main thread once the event
        # loop has fully shut down.  This prevents a race where the background
        # thread and the process manager both restart the bot simultaneously.
        self.bot.restart_requested = True
        await self.bot.close()

    @update.error
    async def update_error(self, ctx, error):
        """Handle permission errors for update command"""
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
        self.restart_requested = False

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
            discord.Activity(type=discord.ActivityType.listening, name="who am i?"),
            discord.Activity(type=discord.ActivityType.listening, name="try asking me question."),
            discord.Activity(type=discord.ActivityType.listening, name="have you seen Game of Thrones?"),
        ])

    async def setup_hook(self):
        """Setup bot cogs and sync commands"""
        _init_command_claims_db()
        _init_subscriptions_db()

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
                    'find', 'locate', 'where', 'search',  # find and aliases
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

    @tasks.loop(minutes=5)
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

        # Prevent duplicate responses when multiple bot instances share the same
        # token, or when the Discord gateway replays events during reconnects.
        if not _try_claim_command(message.id):
            return

        if Config.LOG_CHANNEL_ID and message.channel.id == Config.LOG_CHANNEL_ID:
            guild = message.guild.name if message.guild else "DM"
            channel = message.channel.name if hasattr(message.channel, 'name') else "DM"
            logger.info(f"[DISCORD {guild} #{channel}] {message.author}: {message.content}")

        # Feed messages from the designated learn channel into the AI chat-log.
        if Config.AI_LEARN_CHANNEL_ID and message.channel.id == Config.AI_LEARN_CHANNEL_ID:
            if message.content and not message.content.startswith(self.command_prefix) and not message.author.bot:
                add_chat_message(message.author.display_name, message.content)

        if Config.FIND_BOT_CHANNEL_ID and message.channel.id == Config.FIND_BOT_CHANNEL_ID:
            if message.content.startswith(self.command_prefix):
                # Extract command name (first word after prefix)
                command_content = message.content[len(self.command_prefix):].strip()
                command_text = command_content.split()[0].lower() if command_content else ""
                
                # Allowed commands in FIND_BOT_CHANNEL
                allowed_commands = {
                    'find', 'locate', 'where', 'search',  # find and aliases
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

        # Auto-reply to direct messages (except explicit bot commands).
        if message.guild is None and not message.content.startswith(self.command_prefix):
            question = message.content.strip()
            if question:
                conv_key = _discord_conv_key(message)
                channel_name = getattr(message.channel, "name", None) or "dm"
                async with message.channel.typing():
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
                    )
                await message.reply(f"🤖: {answer}")
                logger.info(f"[DISCORD] DM auto-reply by {message.author.name}: {question[:80]}")
            return

        # Handle bot mention as an implicit !ask
        if self.user in message.mentions:
            # Strip all @mentions to extract the bare question
            question = MENTION_PATTERN.sub('', message.content).strip()
            conv_key = _discord_conv_key(message)
            channel_name = getattr(message.channel, "name", None)
            async with message.channel.typing():
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
                )
            await message.reply(f"🤖: {answer}")
            logger.info(f"[DISCORD] Mention-ask by {message.author.name}: {question[:80]}")
            return

        # Handle a plain reply to one of the bot's AI responses (no prefix/mention needed).
        # This lets users continue the conversation naturally by just replying.
        if (
            message.reference is not None
            and not message.content.startswith(self.command_prefix)
        ):
            ref = message.reference.resolved
            if ref is None:
                try:
                    ref = await message.channel.fetch_message(message.reference.message_id)
                except Exception:
                    ref = None
            if (
                ref is not None
                and ref.author == self.user
                and ref.content.startswith("🤖")
            ):
                question = message.content.strip()
                if question:
                    conv_key = _discord_conv_key(message)
                    channel_name = getattr(message.channel, "name", None)
                    async with message.channel.typing():
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
                        )
                    await message.reply(f"{answer}")
                    logger.info(f"[DISCORD] Reply-ask by {message.author.name}: {question[:80]}")
                    return

        await self.process_commands(message)