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

class EventsCog(commands.Cog):
    def __init__(self, bot, data_manager, shared_state: SharedCogState):
        self.bot = bot
        self.data_manager = data_manager
        self.shared_state = shared_state


    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Redirect users to /nick command in the designated channel and refresh the sticky status embed."""
        if message.guild is None or message.author.bot:
            return

        # Redirect nickname-submission messages in the designated channel.
        if message.channel.id != NICKNAME_SUBMISSION_CHANNEL_ID:
            return

        # Nuke all user messages in this channel and tell them to use /nick
        try:
            await message.delete()
            logger.info(f"[DISCORD] Redirected {message.author} to /nick in {NICKNAME_SUBMISSION_CHANNEL_ID}")
            
            notice = (
                "Please use the **/nick** command to set your nickname and island name.\n"
                "**Format:** `Character Name | Island Name`\n"
                "**Example:** `/nick nickname:ChoPaeng | ChoPaeng Camp`"
            )
            
            try:
                # Try to DM first to keep the channel clean
                await message.author.send(notice)
            except discord.Forbidden:
                # Fallback: Send to channel, delete after 10 seconds
                await message.channel.send(
                    f"{message.author.mention} {notice}", 
                    delete_after=10.0, 
                    silent=True 
                )

        except discord.Forbidden:
            logger.warning(f"[DISCORD] Missing permissions to smite message from {message.author} in {NICKNAME_SUBMISSION_CHANNEL_ID}")
        except discord.NotFound:
            pass # Already dead
        except Exception as e:
            logger.warning(f"[DISCORD] Error handling message in {NICKNAME_SUBMISSION_CHANNEL_ID}: {e}")
