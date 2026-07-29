"""
Discord Command Bot Module
Handles Discord commands for item and villager search with rich embeds
"""

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

logger = logging.getLogger("DiscordCommandBot")

# Island status check constants
DODO_CODE_PATTERN = re.compile(r'\b[A-HJ-NP-Z0-9]{5}\b')
MENTION_PATTERN = re.compile(r'<@!?\d+>')
ISLAND_HOST_NAME = "chopaeng"
MESSAGE_HISTORY_LIMIT = 30
ISLAND_DOWN_IMAGE_URL = "https://cdn.chopaeng.com/misc/Bot-is-Down.jpg"
ONLINE_DISCORD_STATUSES = {discord.Status.online, discord.Status.idle, discord.Status.dnd}

ISLAND_STATUS_DISPLAY = {
    "ONLINE": ("\U0001F7E2", "Online", "green"),        # 🟢
    "OFFLINE": ("\U0001F534", "Offline", "red"),        # 🔴
    "REFRESHING": ("\U0001F7E1", "Refreshing", "orange"),  # 🟡
}

# Patterns for intercepting island bot responses
ISLAND_VISITORS_PATTERN = re.compile(r"The following visitors are on (.+?):", re.IGNORECASE)
ISLAND_VILLAGERS_PATTERN = re.compile(r"The following villagers are on (.+?):", re.IGNORECASE)
ISLAND_DODO_SENT_PATTERN = re.compile(r".+?:\s*Sent you the dodo code via DM", re.IGNORECASE)
ISLAND_DROP_PATTERN = re.compile(r"Item drop request will be executed momentarily", re.IGNORECASE)
ISLAND_INJECT_QUEUED_PATTERN = re.compile(r"Villager inject request has been added to the queue", re.IGNORECASE)
ISLAND_INJECT_MULTI_QUEUED_PATTERN = re.compile(r"Villager inject request for (\d+) villagers?", re.IGNORECASE)
ISLAND_INJECT_COMPLETE_PATTERN = re.compile(r"(.+?) has been injected by the bot at Index (\d+)", re.IGNORECASE)
VISITOR_LINE_PATTERN = re.compile(r'#\d+:\s*(.+)')
DODO_UPDATE_NOTIFICATION_PATTERN = re.compile(r"\[\d{4}-\d{2}-\d{2}\s+\d{1,2}:\d{2}:\d{2}\s+(?:am|pm)\]\s+The Dodo code for .+ has updated, the new Dodo code is:", re.IGNORECASE)
AVAILABLE_SLOT_TEXT = "available slot"
ISLAND_BOT_INTERCEPT_TIMEOUT = 10  # seconds to wait for island bot response
GIT_OUTPUT_MAX_LENGTH = 1900  # max chars of git output to display in Discord
DODO_XLOG_TIMEOUT = 1800  # seconds to wait for a verified flight before posting the dodo-request xlog

AUTO_REPLY_PATTERNS = [
    # Direct question/help intents
    re.compile(r"\b(?:i\s+(?:have|got)\s+(?:a\s+)?question|quick\s+question)\b", re.IGNORECASE),
    re.compile(r"\b(?:can|could|would)\s+you\s+(?:help|explain|tell|check)\b", re.IGNORECASE),
    re.compile(r"\b(?:i\s+need|need|looking\s+for|want)\s+(?:help|assistance|support|advice|tips?)\b", re.IGNORECASE),
    re.compile(r"^\s*(?:help|help\s+me|support|question)\s*[!.?]*\s*$", re.IGNORECASE),

    # Asking the room
    re.compile(r"\b(?:does|do|did|can|could|would|will|is|are)\s+(?:anyone|anybody|someone|somebody)\s+(?:know|help|explain|have|see)\b", re.IGNORECASE),
    re.compile(r"\b(?:anyone|anybody|someone|somebody)\s+(?:know|help|able\s+to\s+help|have\s+an\s+idea)\b", re.IGNORECASE),

    # How/where/what style questions
    re.compile(r"\bhow\s+(?:do|can|to|should|would)\s+(?:i|we|you)\b", re.IGNORECASE),
    re.compile(r"\bwhere\s+(?:can|do|is|are|should)\s+(?:i|we|you|get|find|go)\b", re.IGNORECASE),
    re.compile(r"\bwhat\s+(?:is|are|do|does|should|happens|if)\b", re.IGNORECASE),
    re.compile(r"\b(?:can|should|do|does|is)\s+(?:i|this|that|it)\b", re.IGNORECASE),
    re.compile(r"\bwhy\s+(?:is|are|do|does|did|can|can't|cant)\b", re.IGNORECASE),

    # Problem/advice seeking
    re.compile(r"\b(?:i'?m|i\s+am|im)\s+(?:stuck|lost|confused|struggling)\b", re.IGNORECASE),
    re.compile(r"\b(?:having|have|got|getting|there'?s|there\s+is)\s+(?:a\s+)?(?:problem|issue|error|trouble)\b", re.IGNORECASE),
    re.compile(r"\b(?:any|some)\s+(?:tips|advice|ideas|suggestions|recommendations)\b", re.IGNORECASE),
    re.compile(r"\b(?:best|fastest|easiest)\s+way\s+to\b", re.IGNORECASE),
]

NICKNAME_SUBMISSION_CHANNEL_ID = 1081147108612124742
FREE_DODO_BOARD_INTERVAL_SECONDS = 60
FREE_DODO_BOARD_EMBEDS_PER_MESSAGE = 10
FREE_DODO_BOARD_MARKER = "Chopaeng Camp™"

ISLAND_REVIVE_BATCH_PATH = os.getenv(
    "ISLAND_RESTART_BAT_PATH",
    r"C:\Users\ChoPaeng\Desktop\Relaunch_Island.bat",
)
DODO_SENT_TIPS = [
    "**DO NOT** share your order code with anyone else. Only the person who placed the order may visit that island. Sharing a code may result in a permanent bot ban.",
    "Free up your home storage before visiting so you can unload full pockets quickly and fly back for another run.",
    "Change your server nickname to the format: `Character Name | Island Name`. This helps the team identify you.",
    "**NEVER** press the minus (-) button to leave. Always walk to the airport and fly home through Orville. Leaving with the minus button may cause the island to crash for other visitors and you may lose items.",
    "NAT Type A or B is required for smooth online play. If you have NAT Type C or D you may experience connection problems — please resolve this before joining.",
    "Do not drop unwanted items on the ground. Trash bins are placed all over each island — please use them. Litter prevents islands from refreshing their item spawns for everyone.",
]

COMMAND_CLAIM_EXPIRY_SECONDS = 300  # 5 minutes

# Trivia game settings
TRIVIA_TIMEOUT = 30  # seconds before revealing the answer automatically

# Pattern for dodo code update announcements to auto-delete in sub-island channels
DODO_UPDATE_PATTERN = re.compile(
    r"The Dodo code for .+? has updated,? the new Dodo code is:?\s*[A-HJ-NP-Z0-9]{5}",
    re.IGNORECASE,
)

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

# Shared database path (project root, used when DB_BACKEND=sqlite)
_DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "chobot.db")


def build_island_status_sticky_payload(offline_islands: list[str]) -> tuple[str, str, str | None, discord.Color]:
    """Build the title/description/body for the sticky island-status embed."""
    normalized_islands = sorted({name.strip() for name in offline_islands if name and str(name).strip()})
    if normalized_islands:
        title = "⚠️ Offline Islands"
        description = "The following islands are currently offline:"
        field_value = "\n".join(f"• {name}" for name in normalized_islands)
        color = discord.Color.red()
    else:
        title = "✅ All Islands Online"
        description = "All monitored islands are currently online."
        field_value = None
        color = discord.Color.green()
    return title, description, field_value, color


def _upsert_bot_status(island_id: str, island_name: str, is_online: bool) -> None:
    """Persist the Discord bot online/offline status for an island to the DB.

    Writes to the ``island_bot_status`` table so that the REST API can expose
    live Discord presence data without making Discord API calls itself.
    """
    try:
        conn = connect_db()
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
        with connect_db() as conn:
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
        with connect_db() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS island_subscriptions (
                    user_id INTEGER NOT NULL,
                    island_clean TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'sub',
                    has_island_access INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (user_id, island_clean, kind)
                )"""
            )
            # Migrate existing databases: add has_island_access column if it doesn't exist
            try:
                conn.execute("ALTER TABLE island_subscriptions ADD COLUMN has_island_access INTEGER NOT NULL DEFAULT 0")
            except Exception:
                pass  # Column already exists
    except Exception as exc:
        logger.error(f"[DISCORD] Failed to init island_subscriptions table: {exc}")


def _init_settings_db() -> None:
    """Create the settings table for general bot configuration."""
    try:
        with connect_db() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )"""
            )
    except Exception as exc:
        logger.error(f"[DISCORD] Failed to init settings table: {exc}")


def _resolve_island_by_channel(self, channel_id: int) -> str | None:
    """Look up the island name whose channel_id matches *channel_id*, straight
    from the `islands` table. Returns None if this channel isn't linked to
    any island (e.g. a general chat channel)."""
    try:
        with connect_db() as conn:
            row = conn.execute(
                "SELECT name FROM islands WHERE channel_id = ?",
                (str(channel_id),),
            ).fetchone()
            return row.get("name") if row else None
    except Exception as exc:
        logger.error(f"[DISCORD] Failed to resolve island for channel {channel_id}: {exc}")
        return None


def _get_setting(key: str, default: str = "") -> str:
    """Retrieve a setting value from the DB."""
    try:
        with connect_db() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            return row[0] if row else default
    except Exception as exc:
        logger.error(f"[DISCORD] Failed to get setting '{key}': {exc}")
        return default


def _set_setting(key: str, value: str) -> None:
    """Save a setting value to the DB."""
    try:
        with connect_db() as conn:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            conn.commit()
    except Exception as exc:
        logger.error(f"[DISCORD] Failed to set setting '{key}': {exc}")


def _add_subscription(user_id: int, island_clean: str, kind: str) -> bool:
    """Subscribe *user_id* to alerts for *island_clean*.

    Returns True if a new row was inserted, False if it already existed.
    """
    try:
        with connect_db() as conn:
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
        with connect_db() as conn:
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
        with connect_db() as conn:
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
        with connect_db() as conn:
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
        with connect_db() as conn:
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


def _get_member_role_ids(member: discord.abc.Snowflake) -> set[str]:
    """Return the set of role IDs for a Discord member-like object."""
    return {
        str(role.id)
        for role in getattr(member, "roles", [])
        if getattr(role, "id", None) is not None
    }


def _is_subscriber_member(member: discord.abc.Snowflake) -> bool:
    """Return whether the member holds any configured subscriber/member role."""
    member_roles = _get_member_role_ids(member)
    subscription_role_ids = set(configured_subscription_role_ids())
    return bool(member_roles & subscription_role_ids)


def _is_mod_member(member: discord.abc.Snowflake) -> bool:
    """Return whether the member holds any configured moderator/admin role."""
    member_roles = _get_member_role_ids(member)
    return is_mod(member_roles)


def _get_accessible_islands(member: discord.abc.Snowflake) -> list[str]:
    """Return a list of sub island names that the member can access."""
    import json
    user_role_ids = _get_member_role_ids(member)
    is_mod_user = is_mod(user_role_ids)
    
    accessible: list[str] = []
    
    try:
        with connect_db() as conn:
            rows = conn.execute("SELECT name, is_visible, cat, type, required_roles, channel_id FROM islands").fetchall()
            for row in rows:
                if row.get("is_visible") is False:
                    continue
                
                req_roles_raw = row.get("required_roles")
                req_roles = json.loads(req_roles_raw) if req_roles_raw else []
                
                info = resolved_island_required_roles(
                    row.get("name"),
                    row.get("cat"),
                    req_roles,
                    row.get("type"),
                    row.get("channel_id")
                )
                
                # If no required roles, it's public.
                if not info.required_roles:
                    accessible.append(row.get("name"))
                    continue
                    
                if is_mod_user:
                    accessible.append(row.get("name"))
                    continue
                    
                if set(info.required_roles) & user_role_ids:
                    accessible.append(row.get("name"))
    except Exception as exc:
        logger.error(f"[DISCORD] Error fetching accessible islands for {member.id}: {exc}")
        
    return accessible


def _discord_conv_key(message: discord.Message) -> str:
    """Return a stable per-user-per-channel key for conversation history."""
    guild_id = message.guild.id if message.guild else "dm"
    return f"discord:{guild_id}:{message.channel.id}:{message.author.id}"

def is_admin_or_senior_mod():
    async def predicate(ctx):
        if ctx.author.guild_permissions.administrator:
            return True

        return any(role.id == Config.SENIOR_MOD_ROLE_ID for role in ctx.author.roles)

    return commands.check(predicate)

# ---------------------------------------------------------------------------
# Trivia UI
# ---------------------------------------------------------------------------

TRIVIA_LETTER = ["🇦", "🇧", "🇨", "🇩"]


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
            label = f"{TRIVIA_LETTER[idx]} {choice}"
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
                    f"**{interaction.user.display_name}** got it! "
                    f"The answer is **{self.question['c'][correct]}**! 🎉"
                )
            else:
                result_text = (
                    f"**{interaction.user.display_name}** answered "
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

class RebootConfirmView(discord.ui.View):
    """Confirm/cancel buttons shown before actually rebooting an island."""

    def __init__(self, author_id: int, timeout: float = 30.0):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.result: bool | None = None  # True = confirmed, False = cancelled, None = timed out
        self.interaction: discord.Interaction | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the person who ran this command can confirm it.",
                ephemeral=True,
            )
            return False
        return True

    def _disable_all(self) -> None:
        for item in self.children:
            item.disabled = True

    @discord.ui.button(label="Reboot", style=discord.ButtonStyle.danger, emoji="🔄")
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.result = True
        self.interaction = interaction
        self._disable_all()
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.result = False
        self.interaction = interaction
        self._disable_all()
        self.stop()

    async def on_timeout(self) -> None:
        self.result = None
        self._disable_all()


__all__ = [name for name in dir() if not name.startswith('_')]
