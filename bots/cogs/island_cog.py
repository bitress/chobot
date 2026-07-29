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

class IslandCog(commands.Cog):
    def __init__(self, bot, data_manager, shared_state: SharedCogState):
        self.bot = bot
        self.data_manager = data_manager
        self.shared_state = shared_state


    def _refresh_order_island_lookup(self) -> None:
        """Refresh the fixed order-bot island lookup."""
        self.shared_state.order_island_lookup = {}
        if Config.ORDER_BOT_CHANNEL_ID and Config.ORDER_BOT_ISLAND:
            self.shared_state.order_island_lookup[clean_text(Config.ORDER_BOT_ISLAND)] = Config.ORDER_BOT_CHANNEL_ID

    async def _compute_island_status(
        self,
        guild: discord.Guild,
        include_sub: bool = True,
        include_free: bool = True,
        include_order: bool = True,
    ) -> dict:
        """Compute sub/free/order island status results.

        Shared by the sticky embed refresher and the /islands command so the
        two never drift out of sync with each other.
        """
        island_bot_role = guild.get_role(Config.ISLAND_BOT_ROLE_ID) if Config.ISLAND_BOT_ROLE_ID else None
        if Config.ISLAND_BOT_ROLE_ID and not island_bot_role:
            logger.warning(f"[DISCORD] ISLAND_BOT_ROLE_ID {Config.ISLAND_BOT_ROLE_ID} not found in guild; bot name matching disabled")

        # --- Sub island results ---
        sub_results: list = []
        sub_online = 0
        if include_sub:
            await self.fetch_islands()
            for island in Config.SUB_ISLANDS:
                island_clean = clean_text(island)
                channel_id = self.shared_state.sub_island_lookup.get(island_clean)

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

                if island_bot and island_bot.status in ONLINE_DISCORD_STATUSES:
                    sub_results.append((island, "✅", "Bot online", channel_id))
                    sub_online += 1
                    continue

                island_up = False
                status_reason = ""
                with connect_db() as conn:
                    row = conn.execute("SELECT dodo_code FROM islands WHERE id = ?", (island_clean,)).fetchone()
                    if row:
                        dodo_code = row.get("dodo_code")
                        if dodo_code and str(dodo_code).strip() not in ["", "00000", "-----", "GETTIN'"]:
                            island_up = True
                            status_reason = "Dodo code active"

                if island_up:
                    sub_results.append((island, "✅", status_reason, channel_id))
                    sub_online += 1
                else:
                    sub_results.append((island, "❌", "No recent activity", channel_id))

        # --- Free island results ---
        free_results: list = []
        free_online = 0
        if include_free:
            await self.fetch_free_islands()
            for island in Config.FREE_ISLANDS:
                island_clean = clean_text(island)
                channel_id = self.shared_state.free_island_lookup.get(island_clean)

                island_bot = None
                if island_bot_role:
                    target = clean_text(f"chobot {island}")
                    for member in island_bot_role.members:
                        if member.bot and clean_text(member.display_name) == target:
                            island_bot = member
                            break

                if island_bot and island_bot.status in ONLINE_DISCORD_STATUSES:
                    free_results.append((island, "✅", "Bot online", channel_id))
                    free_online += 1
                elif island_bot:
                    free_results.append((island, "❌", "Bot offline", channel_id))
                else:
                    free_results.append((island, "❓", "Bot not found", channel_id))

        # --- Order island results ---
        order_results: list = []
        order_online = 0
        if include_order:
            self._refresh_order_island_lookup()
            order_bot_member = None
            if Config.ORDER_BOT_DISCORD_ID:
                order_bot_member = guild.get_member(Config.ORDER_BOT_DISCORD_ID)
                if order_bot_member is None:
                    try:
                        order_bot_member = await guild.fetch_member(Config.ORDER_BOT_DISCORD_ID)
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        order_bot_member = None

            for island in getattr(Config, "ORDER_BOT_ISLANDS", []):
                island_clean = clean_text(island)
                channel_id = self.shared_state.order_island_lookup.get(island_clean)
                display_name = "Sinta"  # order-bot island is always shown as "Sinta"

                if order_bot_member and order_bot_member.status in ONLINE_DISCORD_STATUSES:
                    order_results.append((display_name, "✅", "Bot online", channel_id))
                    order_online += 1
                elif order_bot_member:
                    order_results.append((display_name, "❌", "Bot offline", channel_id))
                else:
                    order_results.append((display_name, "❓", "Bot not found", channel_id))

        return {
            "sub_results": sub_results, "sub_online": sub_online, "sub_total": len(Config.SUB_ISLANDS),
            "free_results": free_results, "free_online": free_online, "free_total": len(Config.FREE_ISLANDS),
            "order_results": order_results, "order_online": order_online,
            "order_total": len(getattr(Config, "ORDER_BOT_ISLANDS", [])),
        }

    @app_commands.command(name="nick", description="Set your ACNH nickname and island name")
    @app_commands.describe(nickname="Format: Character Name | Island Name (e.g. ChoPaeng | ChoPaeng Camp)")
    async def nick_command(self, interaction: discord.Interaction, nickname: str):
        """Slash command to set nickname following the required format."""
        
        # Validate format
        if not is_valid_acnh_nickname(nickname.strip()):
            await interaction.response.send_message(
                "**Invalid Format!**\n"
                f"Please use: `{NICKNAME_FORMAT_EXAMPLE}`\n"
                "Example: `ChoPaeng | ChoPaeng Camp` (ensure you include the pipe `|` symbol)",
                ephemeral=True
            )
            return

        try:
            # Change the user's nickname in the guild
            await interaction.user.edit(nick=nickname.strip())
            await interaction.response.send_message(
                f"Your nickname has been set to: `{nickname.strip()}`",
                ephemeral=True
            )
            
            # Log usage if in the submission channel
            if interaction.channel_id == NICKNAME_SUBMISSION_CHANNEL_ID:
                logger.info(f"[DISCORD] {interaction.user} updated their nickname via /nick: {nickname.strip()}")

        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ **Error:** I don't have permission to change your nickname.\n"
                "This usually happens if you are the Server Owner or have a higher role than the bot.",
                ephemeral=True
            )
        except Exception as e:
            logger.error(f"[DISCORD] Failed to set nickname for {interaction.user}: {e}")
            await interaction.response.send_message(
                "❌ **Error:** Something went wrong while updating your nickname. Please contact staff.",
                ephemeral=True
            )

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
                
                req_roles = []
                for target, overwrite in channel.overwrites.items():
                    can_view = (
                        getattr(overwrite, "view_channel", None) is True
                        or getattr(overwrite, "read_messages", None) is True
                    )
                    if isinstance(target, discord.Role) and can_view:
                        if target.name != "@everyone":
                            req_roles.append(str(target.id))
                
                try:
                    import json
                    with connect_db() as conn:
                        conn.execute(
                            "UPDATE islands SET required_roles = ?, channel_id = ? WHERE UPPER(name) = ?",
                            (json.dumps(req_roles), str(channel.id), island_clean.upper())
                        )
                except Exception as e:
                    logger.error(f"[DISCORD] Failed to save required_roles for {island_clean}: {e}")

        self.shared_state.sub_island_lookup = temp_lookup

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

        self.shared_state.free_island_lookup = temp_lookup

        if fetched_islands:
            Config.FREE_ISLANDS = fetched_islands

        logger.info(f"[DISCORD] Dynamic Free Island Fetch Complete. Found {count} islands.")

    def cog_unload(self):
        """Cleanup on unload"""
        self.island_monitor_loop.cancel()
        self.free_dodo_board_loop.cancel()
        self.island_status_sticky_loop.cancel()

    async def _refresh_island_status_sticky_message(
            self,
            channel: discord.TextChannel | None = None,
            force_repost: bool = False,
        ) -> None:
        async with self.shared_state._island_sticky_lock:
            target_channel = channel or self.bot.get_channel(Config.XLOG_VERBOSE_CHANNEL_ID)
            if not isinstance(target_channel, discord.TextChannel):
                return

            guild = target_channel.guild
            if not guild:
                return

            def _format_channel(island_name: str, channel_id: int | None) -> str:
                if not channel_id:
                    return f"**{island_name}**"
                ch = guild.get_channel(channel_id)
                return f"<#{channel_id}>" if ch else f"**{island_name}**"

            status = await self._compute_island_status(guild)
            sub_results, sub_online, sub_total = status["sub_results"], status["sub_online"], status["sub_total"]
            free_results, free_online, free_total = status["free_results"], status["free_online"], status["free_total"]
            order_results, order_online, order_total = status["order_results"], status["order_online"], status["order_total"]

            combined_online = sub_online + free_online + order_online
            total = sub_total + free_total + order_total
            pct = int((combined_online / total) * 100) if total else 0

            def _progress_bar(filled: int, total: int, length: int = 14) -> str:
                if total == 0:
                    return "░" * length
                filled_blocks = round((filled / total) * length)
                return "▰" * filled_blocks + "▱" * (length - filled_blocks)

            if total == 0:
                color = discord.Color.greyple()
            elif combined_online == total:
                color = discord.Color.green()
            elif combined_online == 0:
                color = discord.Color.red()
            else:
                color = discord.Color.orange()

            embed = discord.Embed(
                title="🏝️ Island Status",
                description=f"{_progress_bar(combined_online, total)}  **{combined_online}/{total}** online ({pct}%)",
                color=color,
                timestamp=discord.utils.utcnow(),
            )

            sub_off = [(n, c) for n, s, _, c in sub_results if s != "✅"]
            free_off = [(n, c) for n, s, _, c in free_results if s != "✅"]
            order_off = [(n, c) for n, s, _, c in order_results if s != "✅"]
            all_off = (
                [(n, c, "🏝️") for n, c in sub_off]
                + [(n, c, "🌴") for n, c in free_off]
                + [(n, c, "📦") for n, c in order_off]
            )

            if all_off:
                down_lines = [f"🔴 {tag} {_format_channel(n, c)}" for n, c, tag in all_off]
                down_value = "\n".join(down_lines)
                if len(down_value) > 1024:
                    down_value = down_value[:1000].rsplit("\n", 1)[0] + f"\n…and {len(down_lines) - down_value[:1000].rsplit(chr(10), 1)[0].count(chr(10)) - 1} more"
                embed.add_field(
                    name=f"{Config.EMOJI_FAIL} Needs attention ({len(all_off)})",
                    value=down_value,
                    inline=False,
                )
            else:
                embed.add_field(name="All clear", value="Every island is active.", inline=False)

            embed.add_field(name=f"{Config.STAR_PINK} Sub", value=f"**{sub_online}**/{sub_total} online", inline=True)
            embed.add_field(name=f"🌴 Free", value=f"**{free_online}**/{free_total} online", inline=True)
            embed.add_field(name=f"📦 Order", value=f"**{order_online}**/{order_total} online", inline=True)

            footer_icon_url = guild.icon.url if guild.icon else Config.DEFAULT_PFP
            embed.set_footer(text="Chopaeng Camp™", icon_url=footer_icon_url)
            embed.set_image(url=Config.FOOTER_LINE)

            previous_message = self.shared_state.island_status_sticky_message

            if force_repost:
                if previous_message and previous_message.channel.id == target_channel.id:
                    try:
                        await previous_message.delete()
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                        logger.warning(f"[DISCORD] Could not delete previous sticky island status message: {exc}")
                    self.shared_state.island_status_sticky_message = None
                    previous_message = None
            else:
                if previous_message and previous_message.channel.id == target_channel.id:
                    try:
                        await previous_message.edit(embed=embed)
                        return
                    except (discord.NotFound, discord.HTTPException) as exc:
                        logger.warning(f"[DISCORD] Previous sticky island status message unavailable, sending a new one: {exc}")
                        self.shared_state.island_status_sticky_message = None
                        previous_message = None

            if previous_message is None:
                try:
                    async for msg in target_channel.history(limit=100):
                        if msg.author.id != self.bot.user.id:
                            continue
                        if not msg.embeds:
                            continue
                        embed_obj = msg.embeds[0]
                        if not embed_obj.footer or not embed_obj.footer.text:
                            continue
                        footer_text = embed_obj.footer.text
                        if footer_text.startswith("Island status") or footer_text.startswith("This message is kept pinned"):
                            try:
                                await msg.delete()
                            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                                pass
                except (discord.Forbidden, discord.HTTPException) as exc:
                    logger.warning(f"[DISCORD] Failed to clean old island status sticky messages in {target_channel.id}: {exc}")

            try:
                message = await target_channel.send(embed=embed, silent=True)
                self.shared_state.island_status_sticky_message = message
            except Exception as exc:
                logger.warning(f"[DISCORD] Failed to refresh island status sticky embed in {target_channel.id}: {exc}")

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

    async def _fetch_islands_api_data(self) -> tuple[list[dict], datetime | None] | tuple[None, None]:
        """Fetch raw island snapshot data from the API."""
        url = f"{Config.API_BASE_URL}/api/islands"

        def _get_json():
            response = requests.get(url, timeout=8)
            response.raise_for_status()
            return response.json()

        try:
            payload = await asyncio.to_thread(_get_json)
        except Exception as exc:
            logger.warning(f"[DISCORD] island API request failed: {exc}")
            return None, None

        data = payload.get("data", []) if isinstance(payload, dict) else []
        if not isinstance(data, list):
            data = []
        meta = payload.get("meta", {}) if isinstance(payload, dict) else {}
        api_timestamp = self._parse_iso8601(meta.get("timestamp")) if isinstance(meta, dict) else None
        return data, api_timestamp

    async def _fetch_islands_api_snapshot(self) -> tuple[dict[str, dict], datetime | None] | tuple[None, None]:
        """Fetch island snapshot from the API and return map keyed by cleaned island name."""
        data, api_timestamp = await self._fetch_islands_api_data()
        if data is None:
            return None, None

        island_map: dict[str, dict] = {}
        for item in data:
            name = item.get("name") if isinstance(item, dict) else None
            if not name:
                continue
            normalized = clean_text(name)
            canonical = re.sub(r'^\d+', '', normalized)
            island_map[canonical or normalized] = item

        return island_map, api_timestamp

    @staticmethod
    def _read_first_line(path: str) -> str | None:
        """Read a small status file, retrying once if the island bot has it locked."""
        for attempt in range(2):
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    return f.read().strip()
            except OSError:
                if attempt == 0:
                    time.sleep(0.05)
        return None

    @staticmethod
    def _parse_visitor_count(value: object) -> str:
        """Normalize visitor values for display."""
        if value is None:
            return "0/7"
        text = str(value).strip()
        if not text:
            return "0/7"
        if text.upper() == "FULL":
            return "FULL"
        if text.isdigit():
            return f"{max(0, min(7, int(text)))}/7"
        if re.fullmatch(r"\d+\s*/\s*7", text):
            return text.replace(" ", "")
        return text[:32]

    @staticmethod
    def _resolve_island_status(record: dict) -> tuple[str, str, "discord.Color"]:
        """Map an island API record's status to (icon, label, embed color)."""
        status = str(record.get("status") or "").strip().upper()
        dodo_code = str(record.get("dodo_code") or "").strip().upper()

        if status not in ISLAND_STATUS_DISPLAY:
            # Fall back to inferring status from the dodo code when the API
            # doesn't supply a normalized status string.
            if not dodo_code or dodo_code in ("GETTIN'", "00000", "-----"):
                status = "REFRESHING"
            elif dodo_code == "FULL":
                status = "ONLINE"
            else:
                status = "ONLINE" if DODO_CODE_PATTERN.fullmatch(dodo_code) else "OFFLINE"

        icon, label, color_name = ISLAND_STATUS_DISPLAY.get(status, ISLAND_STATUS_DISPLAY["OFFLINE"])
        color = {
            "green": discord.Color.green(),
            "red": discord.Color.red(),
            "orange": discord.Color.orange(),
        }[color_name]
        return icon, label, color

    def _items_on_island(self, island_clean: str, limit: int = 40) -> list[str]:
        """Reverse-lookup item display names currently available on *island_clean*.

        Only used as a fallback when the island API record doesn't already
        include an `items` list of its own.
        """
        with self.data_manager.lock:
            cache = self.data_manager.cache
            display_map = cache.get("_display", {})
            snapshot = {k: v for k, v in cache.items() if not k.startswith("_")}

        found = []
        for key, locations in snapshot.items():
            if not locations:
                continue
            loc_keys = {clean_text(loc) for loc in str(locations).split(", ")}
            if island_clean in loc_keys:
                found.append(display_map.get(key, key.title()))
                if len(found) >= limit:
                    break
        return sorted(found)

    def _villagers_on_island(self, island_clean: str) -> list[str]:
        """Reverse-lookup villager display names currently residing on *island_clean*."""
        villager_map = self.data_manager.get_villagers([
            Config.VILLAGERS_DIR,
            Config.TWITCH_VILLAGERS_DIR,
        ])

        found = []
        for key, locations in villager_map.items():
            if not locations:
                continue
            loc_keys = {clean_text(loc) for loc in str(locations).split(", ")}
            if island_clean in loc_keys:
                found.append(key.title())
        return sorted(found)

    async def _build_island_info_embed(self, ctx, record: dict, island_clean: str) -> discord.Embed:
        """Build the full island-details embed for /island."""
        raw_name = str(record.get("name") or island_clean).strip()
        display_name = raw_name.title()
        description = (str(record.get("description") or "").strip()
                    or "No description available for this island yet.")
        map_url = str(record.get("map_url") or "").strip()
        visitors = self._parse_visitor_count(record.get("visitors"))

        cat = str(record.get("cat") or record.get("type") or "").strip().lower()
        access_label = (
            "Order Bot" if cat == "order"
            else "Subscribers Only" if cat == "member"
            else "Public"
        )

        icon, status_label, color = self._resolve_island_status(record)
        island_url = f"https://www.chopaeng.com/island/{raw_name.lower()}"

        embed = discord.Embed(
            title=f"\U0001F3DD\uFE0F {display_name}",
            url=island_url,
            description=description[:2000],
            color=color,
            timestamp=discord.utils.utcnow(),
        )

        embed.add_field(name="Status", value=f"{icon} {status_label}", inline=True)
        embed.add_field(name="Passengers", value=visitors, inline=True)
        embed.add_field(name="Gate Type", value=access_label, inline=True)

        # Items — prefer the API record's own list; fall back to the item cache.
        items = record.get("items")
        if not isinstance(items, list) or not items:
            items = await asyncio.to_thread(self._items_on_island, island_clean)
        if items:
            lines = [f"\u2022 {item}" for item in items]
            chunks = self._chunk_lines(lines)
            for i, chunk in enumerate(chunks):
                label = f"Available Loot ({len(items)})" if i == 0 else "Available Loot (cont.)"
                embed.add_field(name=label, value=chunk, inline=False)
        else:
            embed.add_field(name="Available Loot", value="*No items currently tracked.*", inline=False)

        # Villagers / current residents
        villagers = await asyncio.to_thread(self._villagers_on_island, island_clean)
        if villagers:
            lines = [f"\U0001F3E0 {v}" for v in villagers]
            chunks = self._chunk_lines(lines)
            for i, chunk in enumerate(chunks):
                label = f"Current Residents ({len(villagers)})" if i == 0 else "Current Residents (cont.)"
                embed.add_field(name=label, value=chunk, inline=False)
        else:
            embed.add_field(name="Current Residents", value="*No residents currently tracked.*", inline=False)

        if map_url:
            embed.set_image(url=map_url)
            embed.set_thumbnail(url=map_url)  # harmless if Discord ignores the duplicate

        pfp_url = ctx.author.avatar.url if ctx.author.avatar else Config.DEFAULT_PFP
        embed.set_footer(text=f"Requested by {ctx.author.display_name} \u2022 Chopaeng Camp\u2122", icon_url=pfp_url)

        return embed

    def _read_free_dodo_files(self) -> list[dict]:
        """Fallback reader for free-island Dodo files when the API is unavailable."""
        base_dir = Config.DIR_FREE
        if not base_dir or not os.path.exists(base_dir):
            return []

        items = []
        with os.scandir(base_dir) as entries:
            for entry in entries:
                if not entry.is_dir():
                    continue

                raw_dodo = self._read_first_line(os.path.join(entry.path, "Dodo.txt"))
                raw_visitors = self._read_first_line(os.path.join(entry.path, "Visitors.txt"))
                dodo_code = None
                status = "OFFLINE"

                if raw_dodo in ("00000", "-----", ""):
                    status = "REFRESHING"
                elif raw_dodo and DODO_CODE_PATTERN.fullmatch(raw_dodo.strip().upper()):
                    dodo_code = raw_dodo.strip().upper()
                    status = "ONLINE"

                items.append({
                    "name": entry.name.upper(),
                    "dodo_code": dodo_code,
                    "visitors": self._parse_visitor_count(raw_visitors),
                    "status": status,
                    "type": "Free",
                })

        return sorted(items, key=lambda item: str(item.get("name", "")))

    async def _fetch_free_dodo_board_data(self) -> tuple[list[dict], datetime | None]:
        """Return free-island data for the Dodo board."""
        data, api_timestamp = await self._fetch_islands_api_data()
        if data is not None:
            known_free = {clean_text(name) for name in Config.FREE_ISLANDS}
            known_free.update(self.shared_state.free_island_lookup.keys())
            free_items = []
            for item in data:
                if not isinstance(item, dict):
                    continue
                island_type = str(item.get("type", "")).strip().lower()
                island_clean = clean_text(str(item.get("name", "")))
                if island_type == "free" or island_clean in known_free:
                    free_items.append(item)
            return sorted(free_items, key=lambda item: str(item.get("name", ""))), api_timestamp

        file_items = await asyncio.to_thread(self._read_free_dodo_files)
        return file_items, None

    def _build_free_dodo_embed(
        self,
        item: dict,
        checked_at: datetime,
        footer_icon_url: str | None = None,
    ) -> discord.Embed:
        """Build one free-island Dodo board embed."""
        raw_name = str(item.get("name") or "Unknown Island").strip()
        display_name = raw_name.title()
        raw_code = str(item.get("dodo_code") or "").strip().upper()
        status = str(item.get("status") or "UNKNOWN").strip().upper()
        visitors = self._parse_visitor_count(item.get("visitors"))
        map_url = str(item.get("map_url") or "").strip()
        description = str(item.get("description") or "").strip()
        dodo_code = raw_code if DODO_CODE_PATTERN.fullmatch(raw_code) else raw_code

        island_url = "https://www.chopaeng.com/island/"+raw_name.lower()    

        if dodo_code:
            color = discord.Color.green()
            code_line = f"```yaml\n{dodo_code}```"
            status_line = "Online"
        elif dodo_code == "GETTIN'":
            color = discord.Color.orange()
            code_line = "*Refreshing*"
            status_line = "Refreshing"
        else:
            color = discord.Color.red()
            code_line = "*Offline*"
            status_line = "Offline"

        details = []
        if description:
            details.append(description[:180])
        
        links = [f"\n[View Island]({island_url})"]
        if map_url:
            links.append(f"[View Map]({map_url})")
        details.append(" • ".join(links))

        embed = discord.Embed(
            title=f"{display_name}",
            url=island_url or None,
            description="\n".join(details),
            color=color,
            timestamp=checked_at,
        )
        embed.add_field(name="Dodo Code", value=code_line, inline=True)
        embed.add_field(name="Visitors", value=visitors, inline=True)
        embed.add_field(name="Status", value=status_line, inline=True)
        if map_url:
            embed.set_thumbnail(url=map_url)
        embed.set_image(url=Config.FOOTER_LINE)
        embed.set_footer(
            text=f"{FREE_DODO_BOARD_MARKER} • {raw_name}",
            icon_url=footer_icon_url,
        )
        return embed

    def _build_free_dodo_empty_embed(
        self,
        checked_at: datetime,
        footer_icon_url: str | None = None,
    ) -> discord.Embed:
        embed = discord.Embed(
            title="Free Island Dodo Board",
            description="No free island Dodo codes are available right now.",
            color=discord.Color.orange(),
            timestamp=checked_at,
        )
        embed.set_image(url=Config.FOOTER_LINE)
        embed.set_footer(
            text=f"{FREE_DODO_BOARD_MARKER}",
            icon_url=footer_icon_url,
        )
        return embed

    @staticmethod
    def _free_dodo_embed_fingerprint(embed: discord.Embed) -> str:
        payload = embed.to_dict()
        payload.pop("timestamp", None)
        return repr(payload)

    async def _load_existing_free_dodo_board_messages(self, channel: discord.TextChannel) -> None:
        """Find board messages from the current bot after a restart."""
        if self.shared_state.free_dodo_board_messages:
            return
        if not self.bot.user:
            return

        messages = []
        try:
            async for msg in channel.history(limit=50):
                if msg.author.id != self.bot.user.id or not msg.embeds:
                    continue
                footer = msg.embeds[0].footer.text if msg.embeds[0].footer else ""
                if footer and FREE_DODO_BOARD_MARKER in footer:
                    messages.append(msg)
        except discord.Forbidden:
            logger.warning(f"[DISCORD] Missing permission to read Free Dodo board history in #{channel.name}")
            return
        except discord.HTTPException as exc:
            logger.warning(f"[DISCORD] Failed to read Free Dodo board history in #{channel.name}: {exc}")
            return

        messages.sort(key=lambda msg: msg.created_at)
        self.shared_state.free_dodo_board_messages = messages

    async def _delete_existing_free_dodo_board_messages(self, channel: discord.TextChannel) -> None:
        """Delete stale Free Dodo board messages left behind by a previous bot process."""
        if not self.bot.user:
            return

        deleted = 0
        messages_to_delete = []
        try:
            async for msg in channel.history(limit=100):
                if msg.author.id != self.bot.user.id or not msg.embeds:
                    continue
                footer = msg.embeds[0].footer.text if msg.embeds[0].footer else ""
                if not footer or FREE_DODO_BOARD_MARKER not in footer:
                    continue
                messages_to_delete.append(msg)
        except discord.Forbidden:
            logger.warning(f"[DISCORD] Missing permission to delete old Free Dodo board messages in #{channel.name}")
            return
        except discord.HTTPException as exc:
            logger.warning(f"[DISCORD] Failed while deleting old Free Dodo board messages in #{channel.name}: {exc}")
            return

        # Bulk delete collected messages
        batch_size = 100
        for i in range(0, len(messages_to_delete), batch_size):
            batch = messages_to_delete[i:i + batch_size]
            try:
                await channel.delete_messages(batch)
                deleted += len(batch)
            except discord.Forbidden:
                # Fallback to individual deletion
                for msg in batch:
                    try:
                        await msg.delete()
                        deleted += 1
                    except discord.NotFound:
                        pass
                    except Exception as e:
                        logger.warning(f"[DISCORD] Failed to delete message: {e}")
            except Exception as e:
                logger.warning(f"[DISCORD] Failed to bulk delete batch: {e}")

        self.shared_state.free_dodo_board_messages = []
        self.shared_state.free_dodo_board_fingerprints = []
        if deleted:
            logger.info(f"[DISCORD] Deleted {deleted} stale Free Dodo board message(s) in #{channel.name}")

    async def _load_existing_island_status_sticky_message(self, channel: discord.TextChannel) -> None:
        """Find this bot's existing island-status sticky message after a restart, so we
        edit it in place instead of accidentally creating duplicates."""
        if self.shared_state.island_status_sticky_message:
            return
        if not self.bot.user:
            return

        try:
            async for msg in channel.history(limit=50):
                if msg.author.id != self.bot.user.id or not msg.embeds:
                    continue
                embed_obj = msg.embeds[0]
                footer_text = embed_obj.footer.text if embed_obj.footer else ""
                if footer_text and footer_text.startswith("Island status"):
                    self.shared_state.island_status_sticky_message = msg
                    return
        except discord.Forbidden:
            logger.warning(f"[DISCORD] Missing permission to read island status history in #{channel.name}")
        except discord.HTTPException as exc:
            logger.warning(f"[DISCORD] Failed to read island status history in #{channel.name}: {exc}")

    async def _delete_existing_island_status_sticky_messages(self, channel: discord.TextChannel) -> None:
        """Delete stale island-status sticky messages left behind by a previous bot process,
        so a restart always starts from a clean slate (same pattern as the Free Dodo board)."""
        if not self.bot.user:
            return

        deleted = 0
        messages_to_delete = []
        try:
            async for msg in channel.history(limit=100):
                if msg.author.id != self.bot.user.id or not msg.embeds:
                    continue
                embed_obj = msg.embeds[0]
                footer_text = embed_obj.footer.text if embed_obj.footer else ""
                if footer_text and (
                    footer_text.startswith("Island status")
                    or footer_text.startswith("This message is kept pinned")
                ):
                    messages_to_delete.append(msg)
        except discord.Forbidden:
            logger.warning(f"[DISCORD] Missing permission to delete old island status messages in #{channel.name}")
            return
        except discord.HTTPException as exc:
            logger.warning(f"[DISCORD] Failed while deleting old island status messages in #{channel.name}: {exc}")
            return

        batch_size = 100
        for i in range(0, len(messages_to_delete), batch_size):
            batch = messages_to_delete[i:i + batch_size]
            try:
                await channel.delete_messages(batch)
                deleted += len(batch)
            except discord.Forbidden:
                for msg in batch:
                    try:
                        await msg.delete()
                        deleted += 1
                    except discord.NotFound:
                        pass
                    except Exception as e:
                        logger.warning(f"[DISCORD] Failed to delete message: {e}")
            except Exception as e:
                logger.warning(f"[DISCORD] Failed to bulk delete batch: {e}")

        self.shared_state.island_status_sticky_message = None
        if deleted:
            logger.info(f"[DISCORD] Deleted {deleted} stale island status message(s) in #{channel.name}")

    async def _publish_free_dodo_board(self, channel: discord.TextChannel, embeds: list[discord.Embed]) -> None:
        """Edit or create individual Discord messages for each Free Dodo board entry."""
        await self._load_existing_free_dodo_board_messages(channel)
        expected_count = len(embeds)
        fingerprints = [self._free_dodo_embed_fingerprint(embed) for embed in embeds]

        for idx, embed in enumerate(embeds):
            try:
                if idx < len(self.shared_state.free_dodo_board_messages):
                    if (
                        idx < len(self.shared_state.free_dodo_board_fingerprints)
                        and self.shared_state.free_dodo_board_fingerprints[idx] == fingerprints[idx]
                    ):
                        continue
                    await self.shared_state.free_dodo_board_messages[idx].edit(content=None, embeds=[embed])
                    if idx < len(self.shared_state.free_dodo_board_fingerprints):
                        self.shared_state.free_dodo_board_fingerprints[idx] = fingerprints[idx]
                    else:
                        self.shared_state.free_dodo_board_fingerprints.append(fingerprints[idx])
                else:
                    msg = await channel.send(embed=embed)
                    self.shared_state.free_dodo_board_messages.append(msg)
                    self.shared_state.free_dodo_board_fingerprints.append(fingerprints[idx])
            except discord.NotFound:
                self.shared_state.free_dodo_board_messages = []
                self.shared_state.free_dodo_board_fingerprints = []
                logger.warning("[DISCORD] Free Dodo board message disappeared; it will be recreated next cycle.")
                return
            except discord.Forbidden:
                logger.warning(f"[DISCORD] Missing permission to update Free Dodo board in #{channel.name}")
                return
            except discord.HTTPException as exc:
                logger.warning(f"[DISCORD] Failed to update Free Dodo board in #{channel.name}: {exc}")
                return

        extras = self.shared_state.free_dodo_board_messages[expected_count:]
        self.shared_state.free_dodo_board_messages = self.shared_state.free_dodo_board_messages[:expected_count]
        self.shared_state.free_dodo_board_fingerprints = self.shared_state.free_dodo_board_fingerprints[:expected_count]
        for msg in extras:
            try:
                await msg.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

    def check_cooldown(self, user_id: str, cooldown_sec: int = 3) -> bool:
        """Check if user is on cooldown"""
        now = time.time()
        if user_id in self.shared_state.cooldowns:
            if now - self.shared_state.cooldowns[user_id] < cooldown_sec:
                return True
        self.shared_state.cooldowns[user_id] = now

        # Periodic cleanup: prune entries older than 60s every 100 entries
        if len(self.shared_state.cooldowns) > 100:
            self.shared_state.cooldowns = {k: v for k, v in self.shared_state.cooldowns.items() if now - v < 60}

        return False

    def get_island_channel_link(self, island_name):
        """Get channel link for an island with robust fallback search"""
        island_clean = clean_text(island_name)
        if not island_clean:
            return f"**{island_name.title()}**"
        
        # First check our cached lookup
        if island_clean in self.shared_state.sub_island_lookup:
            return f"<#{self.shared_state.sub_island_lookup[island_clean]}>"
        
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
                        self.shared_state.sub_island_lookup[island_clean] = channel.id
                        return f"<#{channel.id}>"
        
        # If no channel found, return bold text
        return f"**{island_name.title()}**"

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
                "`!villagers` - Check current villagers on this sub island\n"
                "`!drop` - Request item drop on this sub island\n"
                "`!injectvillager <name>` - Inject a villager onto this sub island\n"
                "`!mvi <name> [names...]` - Inject multiple villagers onto this sub island\n"
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
                "`!update` - Pull latest code from git and restart the bot (Admin only)\n"
                "`!restart` - Restart the bot without pulling updates (Admin only)"
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

    

    @staticmethod
    def _progress_bar(filled: int, total: int, length: int = 14) -> str:
        if total == 0:
            return "░" * length
        filled_blocks = round((filled / total) * length)
        return "▰" * filled_blocks + "▱" * (length - filled_blocks)

    @staticmethod
    def _problem_lines(results: list, format_channel) -> list[str]:
        """Return formatted lines for only the non-online entries in a results list."""
        lines = []
        for n, s, _, c in results:
            if s == "✅":
                continue  # <-- skip islands that are actually online
            icon = "🔴" if s == "❌" else "❓"
            lines.append(f"{icon} {format_channel(n, c)}")
        return lines

    @staticmethod
    def _all_lines(results: list, format_channel) -> list[str]:
        """Return formatted lines for every island in a results list, online included.
        Matches the 🟢/🔴/❓ + channel-mention style used in the sticky status embed."""
        lines = []
        for n, s, _, c in results:
            icon = "🟢" if s == "✅" else "🔴" if s == "❌" else "❓"
            lines.append(f"{icon} {format_channel(n, c)}")
        return lines

    def _add_status_fields(
        self,
        embed: discord.Embed,
        name: str,
        emoji: str,
        results: list,
        online: int,
        total: int,
        format_channel,
        view: str,
    ) -> None:
        """Add island status field(s) for one category, formatted per the requested view.

        view == 'all'     -> every island, header shows 'Sub — 19/20' style, like the screenshot.
        view == 'summary' -> only non-online islands ('needs attention').
        """
        if view == "all":
            lines = self._all_lines(results, format_channel)
            if not lines:
                embed.add_field(name=f"{emoji} {name} — 0/0", value="*No islands configured.*", inline=True)
                return
            chunks = self._chunk_lines(lines)
            for i, chunk in enumerate(chunks):
                label = f"{emoji} {name} — {online}/{total}" if i == 0 else f"{emoji} {name} (cont.)"
                embed.add_field(name=label, value=chunk, inline=True)
        else:
            self._add_attention_fields(embed, name, self._problem_lines(results, format_channel))

    @staticmethod
    def _chunk_lines(lines: list[str], limit: int = 1024) -> list[str]:
        """Split a list of lines into chunks that each fit Discord's 1024-char field limit."""
        chunks, current = [], ""
        for line in lines:
            candidate = f"{current}\n{line}" if current else line
            if len(candidate) > limit:
                if current:
                    chunks.append(current)
                current = line
            else:
                current = candidate
        if current:
            chunks.append(current)
        return chunks or ["*none*"]

    def _add_attention_fields(self, embed: discord.Embed, name: str, lines: list[str]) -> None:
        """Add one or more 'needs attention' fields, chunked to fit the 1024-char limit."""
        if not lines:
            embed.add_field(name=f"{Config.STAR_PINK} {name} — All Clear", value="Every island is active.", inline=False)
            return
        chunks = self._chunk_lines(lines)
        for i, chunk in enumerate(chunks):
            label = f"{Config.EMOJI_FAIL} {name} — Needs Attention ({len(lines)})" if i == 0 else f"⚠️ {name} (cont.)"
            embed.add_field(name=label, value=chunk, inline=False)

    @commands.hybrid_command(name="islands", aliases=["islandstatus", "checkislands"])
    @app_commands.describe(
        island="Which islands to check: sub, free, order, or leave blank for all.",
        view="Display mode: 'summary' (problems only, default) or 'all' (every island with status).",
    )
    @app_commands.choices(
        island=[
            app_commands.Choice(name="Sub Islands",   value="sub"),
            app_commands.Choice(name="Free Islands",  value="free"),
            app_commands.Choice(name="Order Islands", value="order"),
        ],
        view=[
            app_commands.Choice(name="Summary (problems only)", value="summary"),
            app_commands.Choice(name="All (every island with status)", value="all"),
        ],
    )
    @is_admin_or_senior_mod()
    async def island_status(self, ctx, island: str = "", view: str = "summary"):
        """Check island bot status. Use 'sub', 'free', 'order', or leave blank for all."""
        await ctx.defer()

        guild = self.bot.get_guild(Config.GUILD_ID)
        if not guild:
            await ctx.reply("Guild not found.")
            return

        kind = island.strip().lower()
        view = view.strip().lower() if view else "summary"

        if kind and kind not in ("sub", "free", "order"):
            await ctx.reply("Usage: `/islands [sub|free|order]`", ephemeral=True)
            return

        if view not in ("summary", "all"):
            await ctx.reply("Usage: view must be 'summary' or 'all'", ephemeral=True)
            return

        show_sub   = kind in ("", "sub")
        show_free  = kind in ("", "free")
        show_order = kind in ("", "order")

        island_bot_role = guild.get_role(Config.ISLAND_BOT_ROLE_ID) if Config.ISLAND_BOT_ROLE_ID else None
        if Config.ISLAND_BOT_ROLE_ID and not island_bot_role:
            logger.warning(f"[DISCORD] ISLAND_BOT_ROLE_ID {Config.ISLAND_BOT_ROLE_ID} not found in guild; bot name matching disabled")

        # --- Sub island results ---
        sub_results: list = []
        sub_online = 0
        if show_sub:
            await self.fetch_islands()
            for isl in Config.SUB_ISLANDS:
                island_clean = clean_text(isl)
                channel_id = self.shared_state.sub_island_lookup.get(island_clean)

                if not channel_id:
                    for ch in guild.channels:
                        if isinstance(ch, discord.TextChannel) and island_clean in clean_text(ch.name):
                            channel_id = ch.id
                            break

                if not channel_id:
                    sub_results.append((isl, "❓", "Channel not found", None))
                    continue

                channel = guild.get_channel(channel_id)
                if not channel:
                    sub_results.append((isl, "❓", "Channel not found", None))
                    continue

                island_bot = None
                if island_bot_role:
                    target = clean_text(f"chobot {isl}")
                    for member in island_bot_role.members:
                        if member.bot and clean_text(member.display_name) == target:
                            island_bot = member
                            break

                if island_bot and island_bot.status in ONLINE_DISCORD_STATUSES:
                    sub_results.append((isl, "✅", "Bot online", channel_id))
                    sub_online += 1
                    continue

                try:
                    messages = [msg async for msg in channel.history(limit=25)]
                except discord.Forbidden:
                    sub_results.append((isl, "❓", "No channel access", channel_id))
                    continue

                island_up = False
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
                    sub_results.append((isl, "✅", status_reason, channel_id))
                    sub_online += 1
                else:
                    sub_results.append((isl, "❌", "No recent activity", channel_id))

        # --- Free island results ---
        free_results: list = []
        free_online = 0
        if show_free:
            await self.fetch_free_islands()
            for isl in Config.FREE_ISLANDS:
                island_clean = clean_text(isl)
                channel_id = self.shared_state.free_island_lookup.get(island_clean)

                island_bot = None
                if island_bot_role:
                    target = clean_text(f"chobot {isl}")
                    for member in island_bot_role.members:
                        if member.bot and clean_text(member.display_name) == target:
                            island_bot = member
                            break

                if island_bot and island_bot.status in ONLINE_DISCORD_STATUSES:
                    free_results.append((isl, "✅", "Bot online", channel_id))
                    free_online += 1
                elif island_bot:
                    free_results.append((isl, "❌", "Bot offline", channel_id))
                else:
                    free_results.append((isl, "❓", "Bot not found", channel_id))

        # --- Order island results ---
        order_results: list = []
        order_online = 0
        if show_order:
            self._refresh_order_island_lookup()
            order_bot_member = None
            if Config.ORDER_BOT_DISCORD_ID:
                order_bot_member = guild.get_member(Config.ORDER_BOT_DISCORD_ID)
                if order_bot_member is None:
                    try:
                        order_bot_member = await guild.fetch_member(Config.ORDER_BOT_DISCORD_ID)
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        order_bot_member = None

            for isl in getattr(Config, "ORDER_BOT_ISLANDS", []):
                island_clean = clean_text(isl)
                channel_id = self.shared_state.order_island_lookup.get(island_clean)
                display_name = "Sinta"

                if order_bot_member and order_bot_member.status in ONLINE_DISCORD_STATUSES:
                    order_results.append((display_name, "✅", "Bot online", channel_id))
                    order_online += 1
                elif order_bot_member:
                    order_results.append((display_name, "❌", "Bot offline", channel_id))
                else:
                    order_results.append((display_name, "❓", "Bot not found", channel_id))

        # --- Build embed(s) ---
        pfp_url = ctx.author.avatar.url if ctx.author.avatar else Config.DEFAULT_PFP

        def format_channel(island_name: str, channel_id: int | None) -> str:
            if not channel_id:
                return f"{island_name}"
            ch = guild.get_channel(channel_id)
            if ch and ch.permissions_for(ctx.author).read_messages:
                return f"<#{channel_id}>"
            return f"{island_name}"

        if kind == "sub":
            total = len(Config.SUB_ISLANDS)
            title = "🏝️ Sub Island Status"
            desc = (
                f"{self._progress_bar(sub_online, total)}  **{sub_online}/{total}** online"
                if view == "summary"
                else f"**{sub_online}/{total}** islands active"
            )
            embed = discord.Embed(
                title=title,
                description=desc,
                color=discord.Color.green() if sub_online == total else (
                    discord.Color.orange() if sub_online > 0 else discord.Color.red()
                ),
                timestamp=discord.utils.utcnow()
            )
            self._add_status_fields(embed, "Sub", "🏝️", sub_results, sub_online, total, format_channel, view)
            embed.set_footer(text=f"Requested by {ctx.author.display_name}", icon_url=pfp_url)
            embed.set_image(url=Config.FOOTER_LINE)
            await ctx.reply(embed=embed)
            logger.info(f"[DISCORD] Sub island status check: {sub_online}/{total} online")

        elif kind == "free":
            total = len(Config.FREE_ISLANDS)
            title = "🌴 Free Island Status"
            desc = (
                f"{self._progress_bar(free_online, total)}  **{free_online}/{total}** online"
                if view == "summary"
                else f"**{free_online}/{total}** islands active"
            )
            embed = discord.Embed(
                title=title,
                description=desc,
                color=discord.Color.green() if free_online == total else (
                    discord.Color.orange() if free_online > 0 else discord.Color.red()
                ),
                timestamp=discord.utils.utcnow()
            )
            self._add_status_fields(embed, "Free", "🌴", free_results, free_online, total, format_channel, view)
            embed.set_footer(text=f"Requested by {ctx.author.display_name}", icon_url=pfp_url)
            embed.set_image(url=Config.FOOTER_LINE)
            await ctx.reply(embed=embed)
            logger.info(f"[DISCORD] Free island status check: {free_online}/{total} online")

        elif kind == "order":
            total = len(getattr(Config, "ORDER_BOT_ISLANDS", []))
            title = "📦 Order Island Status"
            desc = (
                f"{self._progress_bar(order_online, total)}  **{order_online}/{total}** online"
                if view == "summary"
                else f"**{order_online}/{total}** islands active"
            )
            embed = discord.Embed(
                title=title,
                description=desc,
                color=discord.Color.green() if order_online == total else (
                    discord.Color.orange() if order_online > 0 else discord.Color.red()
                ),
                timestamp=discord.utils.utcnow()
            )
            self._add_status_fields(embed, "Order", "📦", order_results, order_online, total, format_channel, view)
            embed.set_footer(text=f"Requested by {ctx.author.display_name}", icon_url=pfp_url)
            embed.set_image(url=Config.FOOTER_LINE)
            await ctx.reply(embed=embed)
            logger.info(f"[DISCORD] Order island status check: {order_online}/{total} online")

        else:
            # Combined embed (no argument)
            sub_total   = len(Config.SUB_ISLANDS)
            free_total  = len(Config.FREE_ISLANDS)
            order_total = len(getattr(Config, "ORDER_BOT_ISLANDS", []))
            total       = sub_total + free_total + order_total
            combined_online = sub_online + free_online + order_online

            if view == "all":
                desc = f"**{combined_online}/{total}** islands active"
            else:
                desc = (
                    f"{self._progress_bar(combined_online, total)}  **{combined_online}/{total}** online\n\n"
                    f"🏝️ Sub `{sub_online}/{sub_total}` • 🌴 Free `{free_online}/{free_total}` • 📦 Order `{order_online}/{order_total}`"
                )

            embed = discord.Embed(
                title="🏝️ Island Status",
                description=desc,
                color=discord.Color.green() if combined_online == total else (
                    discord.Color.orange() if combined_online > 0 else discord.Color.red()
                ),
                timestamp=discord.utils.utcnow()
            )
            self._add_status_fields(embed, "Sub",   "🏝️", sub_results,   sub_online,   sub_total,   format_channel, view)
            self._add_status_fields(embed, "Free",  "🌴", free_results,  free_online,  free_total,  format_channel, view)
            self._add_status_fields(embed, "Order", "📦", order_results, order_online, order_total, format_channel, view)
            embed.set_footer(text=f"Requested by {ctx.author.display_name}", icon_url=pfp_url)
            embed.set_image(url=Config.FOOTER_LINE)
            await ctx.reply(embed=embed)
            logger.info(
                f"[DISCORD] Combined island status: sub {sub_online}/{sub_total}, "
                f"free {free_online}/{free_total}, order {order_online}/{order_total}"
            )

    @commands.hybrid_command(name="senddodo", aliases=["sd"])
    async def send_dodo(self, ctx):
        """Send the dodo code to a user via DM"""
        if self._is_order_island_channel(ctx.channel):
            island_name = Config.ORDER_BOT_ISLAND or "This island"
            await ctx.reply(
                f"{island_name} is an order-bot island. Dodo access is handled by the order bot in this channel, so `!senddodo` / `!sd` is not available here.",
                ephemeral=True,
            )
            return
        if not self._is_sub_island_channel(ctx.channel):
            await ctx.reply("This command can only be used in a sub island channel. Please read the sticky post below carefully and make sure you understand and follow all the <#783677194576330792> before agreeing to them.", ephemeral=True)
            return

        if self.check_cooldown(str(ctx.author.id)):
            return

        guild = self.bot.get_guild(Config.GUILD_ID)
        if not guild or not await self._is_channel_online(guild, ctx.channel):
            await ctx.reply(embed=self._create_island_down_embed(ctx))
            return

        island_bot = self._get_island_bot_for_channel(guild, ctx.channel) if guild else None

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

    @commands.hybrid_command(name="injectvillager", aliases=["iv"])
    async def inject_villager(self, ctx, villager_name: str):
        """Inject a villager onto the sub island"""
        if not self._is_sub_island_channel(ctx.channel):
            await ctx.reply("This command can only be used in a sub island channel.", ephemeral=True)
            return

        if self.check_cooldown(str(ctx.author.id)):
            return

        guild = self.bot.get_guild(Config.GUILD_ID)
        if not guild or not await self._is_channel_online(guild, ctx.channel):
            await ctx.reply(embed=self._create_island_down_embed(ctx))
            return

        island_bot = self._get_island_bot_for_channel(guild, ctx.channel) if guild else None

        # First check for the queued message
        def inject_queued_check(msg):
            return (
                msg.author.id == island_bot.id
                and msg.channel.id == ctx.channel.id
                and ISLAND_INJECT_QUEUED_PATTERN.search(msg.content)
            )

        # Then check for the completion message
        def inject_complete_check(msg):
            return (
                msg.author.id == island_bot.id
                and msg.channel.id == ctx.channel.id
                and ISLAND_INJECT_COMPLETE_PATTERN.search(msg.content)
            )

        try:
            # Wait for the queued confirmation
            queued_msg = await self.bot.wait_for('message', check=inject_queued_check, timeout=ISLAND_BOT_INTERCEPT_TIMEOUT)
            await queued_msg.delete()

            # Wait for the completion message
            complete_msg = await self.bot.wait_for('message', check=inject_complete_check, timeout=ISLAND_BOT_INTERCEPT_TIMEOUT + 10)
            
            match = ISLAND_INJECT_COMPLETE_PATTERN.search(complete_msg.content)
            injected_villager = match.group(1).strip() if match else villager_name
            injected_index = match.group(2) if match else "0"
            
            await complete_msg.delete()
            await ctx.reply(embed=self._build_inject_villager_embed(ctx, injected_villager, injected_index))
            logger.info(f"[DISCORD] Intercepted and redesigned !injectvillager response for {ctx.channel.name}")
        except asyncio.TimeoutError:
            logger.warning(f"[DISCORD] Timeout waiting for island bot !injectvillager response in {ctx.channel.name}")

    @commands.hybrid_command(name="mvi")
    async def multi_inject_villager(self, ctx, villagers: str):
        """Inject multiple villagers onto the sub island"""

        villager_names = [name.strip() for name in villagers.split(",") if name.strip()]

        if not villager_names:
            await ctx.reply("Please provide at least one villager name.", ephemeral=True)
            return

        if not self._is_sub_island_channel(ctx.channel):
            await ctx.reply("This command can only be used in a sub island channel.", ephemeral=True)
            return

        if self.check_cooldown(str(ctx.author.id)):
            return

        guild = self.bot.get_guild(Config.GUILD_ID)
        if not guild or not await self._is_channel_online(guild, ctx.channel):
            await ctx.reply(embed=self._create_island_down_embed(ctx))
            return

        island_bot = self._get_island_bot_for_channel(guild, ctx.channel) if guild else None

        # 🔥 SEND COMMAND TO ISLAND BOT (missing in your code)
        command_str = f"!mvi {' '.join(villager_names)}"
        await ctx.send(command_str)

        def multi_inject_queued_check(msg):
            return (
                msg.author.id == island_bot.id
                and msg.channel.id == ctx.channel.id
                and ISLAND_INJECT_MULTI_QUEUED_PATTERN.search(msg.content)
            )

        def inject_complete_check(msg):
            return (
                msg.author.id == island_bot.id
                and msg.channel.id == ctx.channel.id
                and ISLAND_INJECT_COMPLETE_PATTERN.search(msg.content)
            )

        try:
            # Wait for queued confirmation
            queued_msg = await self.bot.wait_for(
                'message',
                check=multi_inject_queued_check,
                timeout=ISLAND_BOT_INTERCEPT_TIMEOUT
            )

            match = ISLAND_INJECT_MULTI_QUEUED_PATTERN.search(queued_msg.content)
            num_villagers = int(match.group(1)) if match else len(villager_names)

            await queued_msg.delete()

            injected_villagers = []

            for _ in range(num_villagers):
                complete_msg = await self.bot.wait_for(
                    'message',
                    check=inject_complete_check,
                    timeout=ISLAND_BOT_INTERCEPT_TIMEOUT + 10
                )

                match = ISLAND_INJECT_COMPLETE_PATTERN.search(complete_msg.content)
                if match:
                    villager = match.group(1).strip()
                    index = match.group(2)
                    injected_villagers.append((villager, index))

                await complete_msg.delete()

            # fallback if nothing parsed
            if not injected_villagers:
                raise asyncio.TimeoutError

            await ctx.reply(embed=self._build_multi_inject_villager_embed(ctx, injected_villagers))
            logger.info(f"[DISCORD] Intercepted and redesigned !mvi response for {ctx.channel.name}")

        except asyncio.TimeoutError:
            logger.warning(f"[DISCORD] Timeout waiting for island bot !mvi response in {ctx.channel.name}")

    def _get_island_name_for_channel(self, channel: discord.TextChannel) -> str | None:
        """Return the island name for the given sub/order channel, or None if unknown."""
        chan_clean = clean_text(channel.name)
        for island in list(Config.SUB_ISLANDS) + list(getattr(Config, "ORDER_BOT_ISLANDS", [])):
            if clean_text(island) in chan_clean:
                return island
        return None

    def _get_island_bot_for_channel(self, guild: discord.Guild, channel: discord.TextChannel):
        """Return the island bot member for the given channel, or None if not found."""
        island = self._get_island_name_for_channel(channel)
        if not island:
            return None

        target = clean_text(f"chobot {island}")
        island_bot_role = guild.get_role(Config.ISLAND_BOT_ROLE_ID) if Config.ISLAND_BOT_ROLE_ID else None

        if island_bot_role:
            for member in island_bot_role.members:
                if member.bot and clean_text(member.display_name) == target:
                    return member

        for member in guild.members:
            if member.bot and clean_text(member.display_name) == target:
                return member

        return None

    async def _is_channel_online(self, guild: discord.Guild, channel: discord.TextChannel) -> bool:
        """Check if the island channel is online by member status or fallback history."""
        island_name = self._get_island_name_for_channel(channel)
        if not island_name:
            return False

        island_clean = clean_text(island_name)
        if island_clean in self.shared_state.order_island_lookup:
            lookup = self.shared_state.order_island_lookup
        elif island_clean in self.shared_state.free_island_lookup:
            lookup = self.shared_state.free_island_lookup
        else:
            lookup = self.shared_state.sub_island_lookup

        return await self._check_island_online(guild, island_name, lookup=lookup)

    def _is_sub_island_channel(self, channel) -> bool:
        """Return True if the channel belongs to the sub-islands category."""
        if not Config.CATEGORY_ID:
            return False
        return getattr(channel, "category_id", None) == Config.CATEGORY_ID

    def _is_order_island_channel(self, channel) -> bool:
        """Return True for the fixed order-bot island channel."""
        return bool(Config.ORDER_BOT_CHANNEL_ID and getattr(channel, "id", None) == Config.ORDER_BOT_CHANNEL_ID)

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

    def _build_villagers_embed(self, ctx, island_name: str, villagers_list: list) -> discord.Embed:
        """Build a nicely formatted villagers embed from a parsed villager list."""
        total = len(villagers_list)

        villager_display = []
        for i, v in enumerate(villagers_list, 1):
            villager_display.append(f"`#{i}` 🏠 **{v}**")

        color = discord.Color.green() if total > 0 else discord.Color.orange()
        embed = discord.Embed(
            title=f"🏝️ Villagers on {island_name}",
            description="\n".join(villager_display) if villager_display else "*No villager data available.*",
            color=color,
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(
            name="Residents",
            value=f"`{total}/10` villagers",
            inline=False
        )
        pfp_url = ctx.author.avatar.url if ctx.author.avatar else Config.DEFAULT_PFP
        embed.set_footer(text=f"Requested by {ctx.author.display_name}", icon_url=pfp_url)
        embed.set_image(url=Config.FOOTER_LINE)
        return embed

    def _build_drop_embed(self, ctx) -> discord.Embed:
        """Build a nicely formatted item drop embed."""
        embed = discord.Embed(
            title="📦 Item Drop Requested",
            description="Item drop request will be executed momentarily.",
            color=discord.Color.gold(),
            timestamp=discord.utils.utcnow()
        )
        pfp_url = ctx.author.avatar.url if ctx.author.avatar else Config.DEFAULT_PFP
        embed.set_footer(text=f"Requested by {ctx.author.display_name}", icon_url=pfp_url)
        embed.set_image(url=Config.FOOTER_LINE)
        return embed

    def _build_inject_villager_embed(self, ctx, villager_name: str, index: str) -> discord.Embed:
        """Build a nicely formatted villager injection embed."""
        embed = discord.Embed(
            title="✨ Villager Injected",
            description=f"**{villager_name}** has been injected onto the island!",
            color=discord.Color.purple(),
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(
            name="Villager",
            value=f"{villager_name}",
            inline=False
        )
        embed.add_field(
            name="Plot Index",
            value=f"`{index}`",
            inline=False
        )
        pfp_url = ctx.author.avatar.url if ctx.author.avatar else Config.DEFAULT_PFP
        embed.set_footer(text=f"Requested by {ctx.author.display_name}", icon_url=pfp_url)
        embed.set_image(url=Config.FOOTER_LINE)
        return embed

    def _build_multi_inject_villager_embed(self, ctx, injected_villagers: list) -> discord.Embed:
        """Build a nicely formatted embed for multiple villager injections."""
        villager_display = []
        for villager, index in injected_villagers:
            villager_display.append(f"`#{index}` 🏠 **{villager}**")

        embed = discord.Embed(
            title="✨ Villagers Injected",
            description=f"**{len(injected_villagers)}** villagers have been injected onto the island!",
            color=discord.Color.purple(),
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(
            name="Villagers",
            value="\n".join(villager_display) if villager_display else "*No villagers injected.*",
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

        warning = nickname_warning_for(ctx.author.display_name)
        if warning:
            embed.add_field(
                name="Nickname Reminder",
                value=warning,
                inline=False,
            )

        embed.add_field(
            name="<:ChoLove:818216528449241128> Reminder",
            value=random.choice(DODO_SENT_TIPS),
            inline=False,
        )

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
        built.  Defaults to ``self.shared_state.sub_island_lookup``.
        """
        island_clean = clean_text(island)
        order_island_keys = {clean_text(name) for name in getattr(Config, "ORDER_BOT_ISLANDS", [])}
        is_order_island = island_clean in order_island_keys
        effective_lookup = lookup if lookup is not None else self.shared_state.sub_island_lookup
        channel_id = effective_lookup.get(island_clean)
        if not channel_id:
            return False

        channel = guild.get_channel(channel_id)
        if not channel:
            return False

        if (
            Config.ORDER_BOT_DISCORD_ID
            and is_order_island
        ):
            order_bot = guild.get_member(Config.ORDER_BOT_DISCORD_ID)
            if order_bot is None:
                try:
                    order_bot = await guild.fetch_member(Config.ORDER_BOT_DISCORD_ID)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    order_bot = None
            if order_bot:
                if order_bot.status in ONLINE_DISCORD_STATUSES:
                    return True
                logger.info(
                    f"[DISCORD] Order bot {Config.ORDER_BOT_DISCORD_ID} status for {island}: {order_bot.status}"
                )

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
            return island_bot.status in ONLINE_DISCORD_STATUSES

        # Fallback: scan recent channel messages for dodo code / host presence
        try:
            messages = [msg async for msg in channel.history(limit=MESSAGE_HISTORY_LIMIT)]
        except discord.Forbidden:
            return False

        for msg in messages:
            if not msg.author.bot:
                continue
            if is_order_island and Config.ORDER_BOT_DISCORD_ID and msg.author.id != Config.ORDER_BOT_DISCORD_ID:
                continue
            content = msg.content or ""
            if (
                DODO_CODE_PATTERN.search(content)
                or ISLAND_HOST_NAME in content.lower()
                or ISLAND_DROP_PATTERN.search(content)
                or ISLAND_INJECT_QUEUED_PATTERN.search(content)
                or ISLAND_INJECT_MULTI_QUEUED_PATTERN.search(content)
                or ISLAND_INJECT_COMPLETE_PATTERN.search(content)
            ):
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

        island_api_map, _ = await self._fetch_islands_api_snapshot()
        island_meta = island_api_map.get(island_clean) if island_api_map else {}
        map_url = str(island_meta.get("map_url") or "").strip()
        island_page_url = f"https://www.chopaeng.com/island/{island_clean.lower()}"
        island_page_text = f"[View Island Page]({island_page_url})"
        map_link_text = f"[View Map]({map_url})" if map_url else ""

        if online:
            title = "🏝️ Island is Back Up!"
            description = (
                f"**{island_display.title()}** island is back online and ready to visit! 🎉\n"
                f"Head to the island channel and use `!senddodo` or `!sd` to get the Dodo code.\n\n"
                f"{island_page_text}"
                + (f" • {map_link_text}" if map_link_text else "")
            )
            color = discord.Color.green()
        else:
            title = "🏝️ Island is Down"
            description = (
                f"**{island_display.title()}** island has gone **offline**.\n"
                f"You'll be notified again when it comes back up.\n\n"
                f"{island_page_text}"
                + (f" • {map_link_text}" if map_link_text else "")
            )
            color = discord.Color.red()

        embed = discord.Embed(
            title=title,
            description=description,
            color=color,
            timestamp=discord.utils.utcnow(),
            url=island_page_url,
        )
        if map_url:
            embed.set_image(url=map_url)
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

    async def _send_order_island_status_alert(self, channel: discord.TextChannel, island_display: str, online: bool) -> None:
        """Send Sinta/order-bot island status transitions into the configured order channel."""
        if online:
            embed = discord.Embed(
                title="Order Island is Back Online",
                description=(
                    f"**{island_display}** is back online for order-bot visits.\n"
                    "Use the order bot flow in this channel. `!senddodo`, `!sd`, and `!drop` are not used here."
                ),
                color=discord.Color.green(),
                timestamp=discord.utils.utcnow(),
            )
            embed.set_image(url=Config.FOOTER_LINE)
        else:
            embed = discord.Embed(
                title="Order Island is Offline",
                description=f"**{island_display}** is currently offline. Please wait for it to come back up before using the order bot.",
                color=discord.Color.red(),
                timestamp=discord.utils.utcnow(),
            )
            embed.set_image(url=ISLAND_DOWN_IMAGE_URL)
        try:
            await channel.send(embed=embed)
            logger.info(f"[DISCORD] Order island monitor: {island_display} is {'ONLINE' if online else 'OFFLINE'}")
        except Exception as exc:
            logger.error(f"[DISCORD] Failed to send order island status alert for {island_display}: {exc}")

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
                with connect_db() as conn:
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
                with connect_db() as conn:
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

    async def island_name_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete helper: combines sub + free island names."""
        all_islands = sorted(
            set(self.shared_state.sub_island_lookup.keys()) | set(self.shared_state.free_island_lookup.keys())
        )
        current_lower = current.lower()
        matches = [n for n in all_islands if current_lower in n] if current else all_islands
        return [
            app_commands.Choice(name=name.title(), value=name)
            for name in matches[:25]
        ]

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
        if island_clean in self.shared_state.sub_island_lookup:
            kind = "sub"
        elif island_clean in self.shared_state.free_island_lookup:
            kind = "free"
        else:
            # Suggest closest match
            all_islands = sorted(
                set(self.shared_state.sub_island_lookup.keys()) | set(self.shared_state.free_island_lookup.keys())
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

    @commands.hybrid_command(name="island", aliases=["islandinfo", "ii"])
    @app_commands.describe(island="The island to look up. Leave blank to auto-detect from the current channel.")
    @app_commands.autocomplete(island=island_name_autocomplete)
    async def island_info(self, ctx, *, island: str = ""):
        """Show full island details: map, status, visitors, description, items, and residents.

        Run with no arguments inside an island's own channel to auto-detect it;
        otherwise pass a name, e.g. `!island alapaap`.
        """
        if self.check_cooldown(str(ctx.author.id)):
            return

        island_clean = clean_text(island) if island else ""
        auto_detected = False

        if not island_clean:
            auto_name = self._resolve_island_by_channel(ctx.channel.id)
            if not auto_name:
                await ctx.reply(
                    "Usage: `!island <name>` \u2014 e.g. `!island alapaap`\n"
                    "Or just run `!island` with no name inside an island's own channel to look it up automatically.",
                    ephemeral=True,
                )
                return
            island_clean = clean_text(auto_name)
            auto_detected = True

        await ctx.defer()

        island_map, _ = await self._fetch_islands_api_snapshot()
        record = (island_map or {}).get(island_clean)

        if not record:
            if auto_detected:
                # This channel is linked to an island in the DB, but the live API
                # snapshot doesn't have it (e.g. API lag or a renamed island) —
                # say so plainly instead of pretending it's a typo to fuzzy-match.
                await ctx.reply(
                    f"This channel is linked to **{island_clean.title()}**, but I couldn't "
                    "find live data for it right now. Please try again shortly."
                )
                logger.warning(f"[DISCORD] /island auto-detected {island_clean} for channel {ctx.channel.id} but API had no record")
                return

            all_islands = sorted((island_map or {}).keys())
            suggestion = ""
            if all_islands:
                best = process.extractOne(island_clean, all_islands, scorer=fuzz.ratio)
                if best and best[1] >= 60:
                    suggestion = f" Did you mean **{best[0].title()}**?"
            await ctx.reply(f"Island **{island_clean.title()}** not found.{suggestion}")
            logger.info(f"[DISCORD] /island miss: {island_clean}")
            return

        embed = await self._build_island_info_embed(ctx, record, island_clean)
        await ctx.reply(embed=embed)
        logger.info(
            f"[DISCORD] /island lookup by {ctx.author.name}: {island_clean}"
            + (" (auto-detected)" if auto_detected else "")
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

    def _build_revive_embed(
        self,
        *,
        title: str,
        color: discord.Color,
        cleaned: str,
        fleet_label: str,
        description: str | None = None,
        output: str | None = None,
        errout: str | None = None,
        elapsed: float | None = None,
        requester: discord.abc.User | None = None,
    ) -> discord.Embed:
        """Build a consistently styled embed for every stage of the revive flow."""
        embed = discord.Embed(
            title=title,
            description=description,
            color=color,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name=f"Island Name", value=f"{cleaned.upper()}", inline=True)
        embed.add_field(name=f"Island Type", value=fleet_label, inline=True)
        if elapsed is not None:
            embed.add_field(name=f"Duration", value=f"{elapsed:.1f}s", inline=True)

        if output:
            trimmed = output[: self.REVIVE_OUTPUT_MAX_LENGTH]
            suffix = "\n…(truncated)" if len(output) > self.REVIVE_OUTPUT_MAX_LENGTH else ""
            embed.add_field(name="Output", value=f"```\n{trimmed}{suffix}\n```", inline=False)
        if errout:
            trimmed_err = errout[: self.REVIVE_OUTPUT_MAX_LENGTH]
            suffix_err = "\n…(truncated)" if len(errout) > self.REVIVE_OUTPUT_MAX_LENGTH else ""
            embed.add_field(name="Error Output", value=f"```\n{trimmed_err}{suffix_err}\n```", inline=False)

        if requester:
            pfp_url = requester.avatar.url if requester.avatar else Config.DEFAULT_PFP
            embed.set_footer(text=f"Requested by {requester.display_name}", icon_url=pfp_url)
        embed.set_image(url=Config.FOOTER_LINE)
        return embed

    @staticmethod
    async def _send_or_edit(
        existing_msg: discord.Message | None,
        responder,
        embed: discord.Embed,
    ) -> discord.Message:
        """Send a new message via *responder* if none exists yet, otherwise edit *existing_msg*.

        Always returns the resulting message so callers can keep editing it on later steps.
        """
        if existing_msg is not None:
            await existing_msg.edit(embed=embed)
            return existing_msg
        return await responder(embed=embed)

    async def _get_island_online_status(
        self, guild: discord.Guild, island: str, fleet: str
    ) -> bool | None:
        """Check whether *island* is currently online for the given fleet.

        Returns True/False if determinable, or None if the island isn't found
        in any known lookup (so we can't say either way).
        """
        island_clean = clean_text(island)
        if str(fleet) == "2":
            lookup = self.shared_state.sub_island_lookup
        else:
            # Free/Order fleet — check whichever lookup actually has it
            if island_clean in self.shared_state.order_island_lookup:
                lookup = self.shared_state.order_island_lookup
            else:
                lookup = self.shared_state.free_island_lookup

        if island_clean not in lookup:
            return None

        try:
            return await self._check_island_online(guild, island, lookup=lookup)
        except Exception as exc:
            logger.warning(f"[DISCORD] Could not determine online status for {island}: {exc}")
            return None
