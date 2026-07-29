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
from bots.cogs.cog_utils import _set_setting

logger = logging.getLogger("DiscordCommandBot")

class AdminCog(commands.Cog):
    def __init__(self, bot, data_manager, shared_state: SharedCogState):
        self.bot = bot
        self.data_manager = data_manager
        self.shared_state = shared_state


    async def revive_island_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete island names for revive based on the selected fleet."""
        fleet_value = None
        if getattr(interaction, "namespace", None) is not None:
            fleet_value = getattr(interaction.namespace, "fleet", None)

        if fleet_value is None:
            for option in interaction.data.get("options", []):
                if option.get("name") == "fleet":
                    fleet_value = option.get("value")
                    break

        if str(fleet_value) == "2":
            candidates = list(self.shared_state.sub_island_lookup.keys()) or list(getattr(Config, "SUB_ISLANDS", []))
        else:
            candidates = list(
                set(self.shared_state.free_island_lookup.keys())
                | set(self.shared_state.order_island_lookup.keys())
            )
            if not candidates:
                candidates = list(getattr(Config, "FREE_ISLANDS", [])) + list(
                    getattr(Config, "ORDER_BOT_ISLANDS", [])
                )

        current_lower = current.lower()
        matches = [name for name in candidates if current_lower in name] if current else candidates
        return [
            app_commands.Choice(name=name.title(), value=name)
            for name in sorted(matches)[:25]
        ]

    @commands.hybrid_command(name="revive", description="Restart a crashed island")
    @app_commands.describe(
        fleet="Island Type",
        island="Island Name",
    )
    @app_commands.choices(
        fleet=[
            app_commands.Choice(name="Free/Order Island", value="1"),
            app_commands.Choice(name="Sub Island", value="2"),
        ]
    )
    @app_commands.autocomplete(island=revive_island_autocomplete)
    @is_admin_or_senior_mod()
    async def revive(self, ctx, fleet: str, island: str):
        """Restart a crashed island's Dodo session (Admin or Senior Mod only)."""
        fleet_label = {
            "1": "Free/Order Island",
            "2": "Sub Island",
        }.get(str(fleet), str(fleet))

        cleaned = island.strip().replace(" ", "")
        if not cleaned or any(token in cleaned for token in ("..", "/", "\\", '"')):
            await ctx.reply(
                embed=self._build_revive_embed(
                    title="Invalid Island Name",
                    color=discord.Color.red(),
                    cleaned=island.strip() or "*(empty)*",
                    fleet_label=fleet_label,
                    description="Island names can't contain path separators or quotes. Try again with a plain island name.",
                    requester=ctx.author,
                ),
                ephemeral=True,
            )
            return

        interaction = getattr(ctx, "interaction", None)
        if interaction is not None and not interaction.response.is_done():
            await interaction.response.defer(thinking=True)
            responder = interaction.followup.send
        else:
            responder = ctx.reply

        # --- Step 1: check current online status ---
        guild = self.bot.get_guild(Config.GUILD_ID)
        is_online = await self._get_island_online_status(guild, cleaned, fleet) if guild else None

        if is_online is True:
            status_line = "🟢 This island currently appears to be **online**."
        elif is_online is False:
            status_line = "🔴 This island currently appears to be **offline**."
        else:
            status_line = "❓ Could not determine this island's current status."

        # --- Step 2: ask for confirmation ---
        confirm_view = RebootConfirmView(author_id=ctx.author.id)
        status_msg = await responder(
            embed=self._build_revive_embed(
                title="Confirm Reboot",
                color=discord.Color.orange(),
                cleaned=cleaned,
                fleet_label=fleet_label,
                description=(
                    f"{status_line}\n\n"
                    "Rebooting will restart the island regardless of its "
                    "current status. Continue?"
                ),
                requester=ctx.author,
            ),
            view=confirm_view,
        )

        await confirm_view.wait()

        if confirm_view.result is not True:
            cancel_embed = self._build_revive_embed(
                title="Reboot Cancelled" if confirm_view.result is False else "⌛ Confirmation Timed Out",
                color=discord.Color.greyple(),
                cleaned=cleaned,
                fleet_label=fleet_label,
                description="No changes were made." if confirm_view.result is False
                            else "No response received in time — no changes were made.",
                requester=ctx.author,
            )
            if confirm_view.interaction is not None:
                await confirm_view.interaction.response.edit_message(embed=cancel_embed, view=None)
            else:
                await status_msg.edit(embed=cancel_embed, view=None)
            logger.info(
                "[DISCORD] Island reboot %s for %s (%s) by %s",
                "cancelled" if confirm_view.result is False else "timed out",
                cleaned, fleet_label, ctx.author,
            )
            return

        # Acknowledge the button click and drop the view before proceeding
        await confirm_view.interaction.response.edit_message(view=None)

        # --- Step 3: let them know a revive is already queued, if applicable ---
        if self.shared_state._revive_lock.locked():
            await status_msg.edit(
                embed=self._build_revive_embed(
                    title="Reboot Queued",
                    color=discord.Color.orange(),
                    cleaned=cleaned,
                    fleet_label=fleet_label,
                    description="Another revive is currently in progress. This one will run right after it finishes.",
                    requester=ctx.author,
                )
            )

        start = time.monotonic()
        async with self.shared_state._revive_lock:
            await status_msg.edit(
                embed=self._build_revive_embed(
                    title="Rebooting Island…",
                    color=discord.Color.blurple(),
                    cleaned=cleaned,
                    fleet_label=fleet_label,
                    description="Sending restart request. This can take up to a minute.",
                    requester=ctx.author,
                )
            )

            try:
                result = await asyncio.to_thread(
                    subprocess.run,
                    [ISLAND_REVIVE_BATCH_PATH, str(fleet), cleaned],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    shell=True,
                )
            except subprocess.TimeoutExpired:
                elapsed = time.monotonic() - start
                await status_msg.edit(
                    embed=self._build_revive_embed(
                        title="⏱Reboot Timed Out",
                        color=discord.Color.orange(),
                        cleaned=cleaned,
                        fleet_label=fleet_label,
                        description="The restart script didn't finish within 60 seconds. It may still be running in the background — check the island channel before retrying.",
                        elapsed=elapsed,
                        requester=ctx.author,
                    )
                )
                logger.warning(
                    "[DISCORD] Island reboot timed out: %s (%s) requested by %s",
                    cleaned, fleet_label, ctx.author,
                )
                return

        elapsed = time.monotonic() - start
        output = (result.stdout or "").strip()
        errout = (result.stderr or "").strip()

        if result.returncode == 0:
            embed = self._build_revive_embed(
                title="Island Rebooted",
                color=discord.Color.green(),
                cleaned=cleaned,
                fleet_label=fleet_label,
                output=output or None,
                elapsed=elapsed,
                requester=ctx.author,
            )
        else:
            embed = self._build_revive_embed(
                title="❌ Reboot Failed",
                color=discord.Color.red(),
                cleaned=cleaned,
                fleet_label=fleet_label,
                description=f"Script exited with code `{result.returncode}`.",
                output=output or None,
                errout=errout or None,
                elapsed=elapsed,
                requester=ctx.author,
            )

        await status_msg.edit(embed=embed)

        logger.info(
            "[DISCORD] Island revive command invoked by %s for %s (%s), rc=%s, elapsed=%.1fs",
            ctx.author,
            cleaned,
            fleet_label,
            result.returncode,
            elapsed,
        )

    @commands.hybrid_command(name="refresh")
    @is_admin_or_senior_mod()
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
        try:
            backup = create_sqlite_backup("pre_update")
            if backup.get("ok"):
                await ctx.reply(f"Database backup created: `{backup.get('file')}`")
        except Exception as exc:
            logger.warning("[DISCORD] Pre-update backup failed: %s", exc)

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

    @commands.hybrid_command(name="restart")
    @is_admin_or_senior_mod()
    async def restart(self, ctx):
        """Restart the bot without pulling updates (Owners or Senior Mod only)"""
        await ctx.reply("Restarting bot now...")
        logger.info("[DISCORD] Restart requested by %s. Restarting process...", ctx.author)

        self.bot.restart_requested = True
        await self.bot.close()

    @restart.error
    async def restart_error(self, ctx, error):
        """Handle permission errors for restart command"""
        if isinstance(error, commands.MissingPermissions):
            await ctx.reply("You do not have permission to use this command.")

    @commands.hybrid_command(name="autoreply")
    @app_commands.describe(enabled="Enable or disable keyword auto-replies")
    @commands.has_permissions(administrator=True)
    async def autoreply(self, ctx, enabled: bool):
        """Manage autoreply settings (Admin only)"""
        self.bot.autoreply_enabled = enabled
        _set_setting("autoreply_enabled", "1" if enabled else "0")
        
        status = "enabled" if enabled else "disabled"
        await ctx.reply(f"Autoreply {status}. The bot will {'now' if enabled else 'no longer'} respond to keyword triggers.")
        logger.info(f"[DISCORD] Autoreply {status} by {ctx.author.name}")

    @autoreply.error
    async def autoreply_error(self, ctx, error):
        """Handle permission errors for autoreply command"""
        if isinstance(error, commands.MissingPermissions):
            await ctx.reply("You do not have permission to use this command.")

    @commands.hybrid_command(name="clear")
    @app_commands.describe(channel="The channel to clear (defaults to current channel)")
    @commands.has_permissions(administrator=True)
    async def clear(self, ctx, channel: discord.TextChannel = None):
        """Clear all messages from a channel with confirmation (Admin only)"""
        if channel is None:
            channel = ctx.channel
        
        # Check if bot has permission to delete messages in the target channel
        if not channel.permissions_for(ctx.me).manage_messages:
            await ctx.reply(f"I don't have permission to delete messages in {channel.mention}")
            return
        
        # Create confirmation buttons
        class ConfirmView(discord.ui.View):
            def __init__(self, author_id: int, target_channel: discord.TextChannel, bot_cog):
                super().__init__(timeout=30.0)
                self.author_id = author_id
                self.target_channel = target_channel
                self.bot_cog = bot_cog
                self.confirmed = False
            
            async def on_timeout(self):
                for item in self.children:
                    item.disabled = True
            
            @discord.ui.button(label="Confirm", style=discord.ButtonStyle.red)
            async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                if interaction.user.id != self.author_id:
                    await interaction.response.send_message("You don't have permission to use this.", ephemeral=True)
                    return
                
                self.confirmed = True
                await interaction.response.defer()
                
                # Disable buttons
                for item in self.children:
                    item.disabled = True
                await interaction.message.edit(view=self)
                
                # Send progress message
                progress_msg = await ctx.send(f"Clearing all messages from {self.target_channel.mention}...")
                
                deleted_count = 0
                failed_count = 0
                
                try:
                    messages_to_delete = []
                    async for message in self.target_channel.history(limit=None):
                        messages_to_delete.append(message)
                    
                    # Discord only allows bulk delete for messages < 14 days old
                    cutoff_time = datetime.now(timezone.utc) - timedelta(days=13, hours=23, minutes=59)
                    
                    deleted_count = 0
                    failed_count = 0
                    
                    # First pass: bulk delete recent messages (< 14 days)
                    batch_size = 100
                    messages_idx = 0
                    while messages_idx < len(messages_to_delete):
                        batch = []
                        batch_start_idx = messages_idx
                        
                        # Build a batch of messages that are all under 14 days old
                        while messages_idx < len(messages_to_delete) and len(batch) < batch_size:
                            msg = messages_to_delete[messages_idx]
                            if msg.created_at > cutoff_time:
                                batch.append(msg)
                                messages_idx += 1
                            else:
                                # Hit an old message, stop this batch
                                break
                        
                        # If we found recent messages to delete
                        if batch:
                            try:
                                await self.target_channel.delete_messages(batch)
                                deleted_count += len(batch)
                            except Exception as e:
                                # Fallback to individual deletion
                                for msg in batch:
                                    try:
                                        await msg.delete()
                                        deleted_count += 1
                                    except Exception as del_err:
                                        failed_count += 1
                                        logger.warning(f"[DISCORD] Failed to delete message: {del_err}")
                                    await asyncio.sleep(0.05)
                                logger.warning(f"[DISCORD] Bulk delete failed, fell back to individual: {e}")
                            
                            await asyncio.sleep(0.5)
                        else:
                            # No more recent messages, move to old ones
                            break
                    
                    # Second pass: individually delete old messages (>= 14 days)
                    for idx in range(messages_idx, len(messages_to_delete)):
                        msg = messages_to_delete[idx]
                        try:
                            await msg.delete()
                            deleted_count += 1
                        except Exception as e:
                            failed_count += 1
                            logger.warning(f"[DISCORD] Failed to delete old message: {e}")
                        await asyncio.sleep(0.1)
                
                except discord.Forbidden:
                    await progress_msg.edit(content=f"Cannot access message history in {self.target_channel.mention}")
                    logger.error(f"[DISCORD] Cannot access history for channel {self.target_channel.name}")
                    return
                
                # Update progress message
                status_msg = f"Cleared {deleted_count} messages from {self.target_channel.mention}"
                if failed_count > 0:
                    status_msg += f" ({failed_count} failed)"
                await progress_msg.edit(content=status_msg)
                logger.info(f"[DISCORD] Channel {self.target_channel.name} cleared ({deleted_count} deleted, {failed_count} failed)")
            
            @discord.ui.button(label="Cancel", style=discord.ButtonStyle.grey)
            async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
                if interaction.user.id != self.author_id:
                    await interaction.response.send_message("You don't have permission to use this.", ephemeral=True)
                    return
                
                await interaction.response.defer()
                
                # Disable buttons
                for item in self.children:
                    item.disabled = True
                await interaction.message.edit(content="Clear operation cancelled.", view=self)
                logger.info(f"[DISCORD] Clear operation for {self.target_channel.name} cancelled")
        
        view = ConfirmView(ctx.author.id, channel, self)
        await ctx.reply(
            f"**Clear all messages from {channel.mention}?**\n(This action cannot be undone)",
            view=view,
            mention_author=True
        )

    @clear.error
    async def clear_error(self, ctx, error):
        """Handle permission errors for clear command"""
        if isinstance(error, commands.MissingPermissions):
            await ctx.reply("You do not have permission to use this command.")
        elif isinstance(error, commands.BadArgument):
            await ctx.reply("Please provide a valid channel.")
