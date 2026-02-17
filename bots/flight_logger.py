"""
Discord Flight Logger Module
Tracks island visitor arrivals, alerts on unknown travelers, and handles moderation internally.
"""

import re
import logging
import unicodedata
import datetime
import aiosqlite  # Requires: pip install aiosqlite

import discord
from discord.ext import commands, tasks
from discord.ui import View, UserSelect, Select, button
from utils.config import Config

logger = logging.getLogger("FlightLogger")

# --- DATABASE SETUP ---
DB_NAME = "warnings.db"

async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS warnings (
                user_id INTEGER,
                guild_id INTEGER,
                reason TEXT,
                mod_id INTEGER,
                timestamp INTEGER
            )
        """)
        await db.commit()

async def add_warning(user_id, guild_id, reason, mod_id):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT INTO warnings VALUES (?, ?, ?, ?, ?)",
                         (user_id, guild_id, reason, mod_id, int(discord.utils.utcnow().timestamp())))
        await db.commit()

async def get_warn_count(user_id, guild_id):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM warnings WHERE user_id = ? AND guild_id = ?", (user_id, guild_id))
        row = await cursor.fetchone()
        return row[0] if row else 0

# --- CONFIGURATION & TEMPLATES ---

# Flight logger violations are always Sub Rule #2 (entering without being linked)
DEFAULT_REASON_TEXT = (
    "Breaking [Sub Rule #2](https://discord.com/channels/729590421478703135/"
    "783677194576330792/1137904975553499217). We have removed your island access "
    "for now. Please read the <#783677194576330792> again to gain access."
)

DURATION_OPTIONS = [
    discord.SelectOption(label="1 Hour",    value="1h"),
    discord.SelectOption(label="1 Day",     value="1d"),
    discord.SelectOption(label="2 Days",    value="2d"),
    discord.SelectOption(label="3 Days",    value="3d"),
    discord.SelectOption(label="1 Week",    value="1w"),
    discord.SelectOption(label="Permanent", value="perm"),
]

def _build_options_with_default(base_options: list[discord.SelectOption], selected_value: str | None):
    return [
        discord.SelectOption(
            label=opt.label, value=opt.value, description=opt.description,
            default=(opt.value == selected_value)
        )
        for opt in base_options
    ]

def create_sapphire_log(member: discord.Member, mod: discord.Member, reason: str, case_id: str, warn_count: int, duration: str):
    """Generates the visual embed mimicking Sapphire"""
    now = discord.utils.utcnow()
    expiry = now + datetime.timedelta(days=7)
    expiry_ts = int(expiry.timestamp())

    desc_lines = [
        f"> **{member.mention} ({member.display_name})** has been warned!",
        f"> **Reason:** {reason}",
        f"> **Duration:** {duration}",
        f"> **Count:** {warn_count}",
        f"> **Responsible:** {mod.mention} ({mod.top_role.name})",
        f"> Automatically expires <t:{expiry_ts}:R>",
        f"> **Proof:** Verified (Log System)",
        "> ",
        "> **For Sub Members**: Please double check our <#783677194576330792> channel.",
        "> **For Free Members**: Kindly refer to our <#755522711492493342> channel."
    ]

    embed = discord.Embed(
        title=f"**Warning Case ID: {case_id}**",
        description="\n".join(desc_lines),
        color=0xff0000,
        timestamp=now
    )
    embed.set_thumbnail(url="https://i.ibb.co/HXyRH3R/2668-Siren.gif")
    embed.set_footer(text=f"Mod: {mod.display_name}", icon_url=mod.display_avatar.url)
    return embed

# --- UI VIEWS ---

class PunishmentBuilderView(discord.ui.View):
    """
    Two-step builder: Select User → (Optional Duration) → Confirm.
    Kick/Ban actions hide the duration selector.
    """
    def __init__(self, action_type: str, original_view: "TravelerActionView", log_message: discord.Message):
        super().__init__(timeout=120)
        self.action_type    = action_type       # "WARN", "KICK", or "BAN"
        self.original_view  = original_view
        self.log_message    = log_message

        self.selected_member:   discord.Member | None = None
        self.selected_duration: str = "1d" # Default for Warns

        self._rebuild_selects()

    def _rebuild_selects(self):
        self.clear_items()

        # --- Row 0: User Select ---
        user_select = discord.ui.UserSelect(
            placeholder="1. Select the Target User...",
            min_values=1, max_values=1, row=0
        )
        async def _user_cb(interaction: discord.Interaction):
            values = interaction.data.get("values", [])
            if values:
                self.selected_member = interaction.guild.get_member(int(values[0]))
            self._rebuild_selects()
            await interaction.response.edit_message(view=self)
        
        user_select.callback = _user_cb
        self.add_item(user_select)

        # --- Row 1: Duration Select (WARN ONLY) ---
        if self.action_type == "WARN":
            duration_select = discord.ui.Select(
                placeholder="2. Select Duration (default: 1 day)",
                min_values=1, max_values=1, row=1,
                options=_build_options_with_default(DURATION_OPTIONS, self.selected_duration)
            )
            async def _duration_cb(interaction: discord.Interaction):
                values = interaction.data.get("values", [])
                if values:
                    self.selected_duration = values[0]
                self._rebuild_selects()
                await interaction.response.edit_message(view=self)
            
            duration_select.callback = _duration_cb
            self.add_item(duration_select)

        # --- Row 2: Confirm Button ---
        can_submit = self.selected_member is not None
        
        # Determine label
        if self.selected_member:
            label = f"Confirm {self.action_type.title()} on {self.selected_member.display_name}"
        else:
            label = "Confirm Action"

        confirm_btn = discord.ui.Button(
            label=label, style=discord.ButtonStyle.danger, row=2, disabled=not can_submit
        )
        confirm_btn.callback = self.confirm_callback
        self.add_item(confirm_btn)

    async def confirm_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        target      = self.selected_member
        reason_text = DEFAULT_REASON_TEXT
        mod         = interaction.user
        
        # Determine final duration string based on action
        if self.action_type == "BAN":
            final_duration = "Permanent"
        elif self.action_type == "KICK":
            final_duration = "N/A"
        else:
            final_duration = self.selected_duration

        try:
            # 1. Execute Discord Action
            if self.action_type == "KICK":
                await target.kick(reason=f"FlightLog: {reason_text}")
                action_verb = "KICKED"
                color       = 0xF1C40F
            elif self.action_type == "BAN":
                await target.ban(reason=f"FlightLog: {reason_text}")
                action_verb = "BANNED"
                color       = 0x992D22
            else:  # WARN
                # Try to DM the user for warns
                try:
                    await target.send(f"**Warning**\nReason: {reason_text}")
                except:
                    pass 
                action_verb = "WARNED"
                color       = 0xE67E22

            # 2. Database Log
            await add_warning(target.id, interaction.guild.id, reason_text, mod.id)
            new_count = await get_warn_count(target.id, interaction.guild.id)

            # 3. Send Log to Sub Mod Channel
            log_embed = create_sapphire_log(target, mod, reason_text, "AUTO", new_count, final_duration)

            sub_mod_channel = interaction.guild.get_channel(Config.SUB_MOD_CHANNEL_ID)
            if sub_mod_channel:
                await sub_mod_channel.send(content=target.mention, embed=log_embed)
                await interaction.followup.send(
                    f"✅ Action executed and logged in {sub_mod_channel.mention}", ephemeral=True
                )
            else:
                await interaction.followup.send("✅ Action executed. (Log channel not found)", ephemeral=True)

            # 4. Resolve the Flight Logger Alert
            msg_to_mod = f"✅ **{target.display_name}** processed internally ({action_verb})."
            await self.original_view._resolve_alert(
                interaction, action_verb, color, msg_to_mod,
                target_user=target, log_message=self.log_message
            )

        except discord.Forbidden:
            await interaction.followup.send("❌ I do not have permission to kick/ban that user. (Check Role Hierarchy)", ephemeral=True)
        except Exception as e:
            logger.error(f"Action failed: {e}")
            await interaction.followup.send(f"❌ Error executing action: {e}", ephemeral=True)

        self.stop()


class TravelerActionView(discord.ui.View):
    def __init__(self, bot, ign):
        super().__init__(timeout=86400)
        self.bot = bot
        self.ign = ign

    async def _resolve_alert(self, interaction, status_label, color, log_msg, target_user=None, log_message=None):
        target_str      = f"{target_user.mention}" if target_user else "Visitor (unlinked)"
        message_to_edit = log_message or (interaction.message if not interaction.response.is_done() else None)

        if message_to_edit:
            try:
                embed = message_to_edit.embeds[0]
                # Update color and header — leave description, fields, image, and footer untouched
                embed.color = color
                embed.set_author(name=f"CASE CLOSED: {status_label}", icon_url=interaction.user.display_avatar.url)
                # Append the Action Taken field without clearing existing fields
                embed.add_field(
                    name="<:ChoLove:818216528449241128> Action Taken",
                    value=f"**{status_label}** by {interaction.user.mention}\nTarget: {target_str}",
                    inline=False
                )
                for child in self.children:
                    child.disabled = True
                await message_to_edit.edit(embed=embed, view=self)
            except Exception as e:
                logger.error(f"Error editing original message: {e}")

        if interaction.response.is_done():
            await interaction.followup.send(log_msg, ephemeral=True)
        else:
            await interaction.response.send_message(log_msg, ephemeral=True)

    @discord.ui.button(label="Admit", style=discord.ButtonStyle.success, emoji="<:Cho_Check:1456715827213504593>")
    async def confirm_action(self, interaction: discord.Interaction, button: discord.ui.Button):
        msg = f"<:Cho_Check:1456715827213504593> **{self.ign}** is cleared for entry."
        await self._resolve_alert(interaction, "AUTHORIZED", 0x2ECC71, msg)

    @discord.ui.button(label="Warn", style=discord.ButtonStyle.primary, emoji="<:Cho_Warn:1456712416271405188>")
    async def warn_action(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = PunishmentBuilderView("WARN", self, log_message=interaction.message)
        await interaction.response.send_message("🔍 **Build Warning:**", view=view, ephemeral=True)

    @discord.ui.button(label="Kick", style=discord.ButtonStyle.secondary, emoji="🦵")
    async def kick_action(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = PunishmentBuilderView("KICK", self, log_message=interaction.message)
        await interaction.response.send_message("🥾 **Build Kick:**", view=view, ephemeral=True)

    @discord.ui.button(label="Ban", style=discord.ButtonStyle.danger, emoji="<:Cho_Kick:1456714701630214349>")
    async def ban_action(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = PunishmentBuilderView("BAN", self, log_message=interaction.message)
        await interaction.response.send_message("🔨 **Build Ban:**", view=view, ephemeral=True)


# --- MAIN COG ---

class FlightLoggerCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.island_map = {}
        self.join_pattern = re.compile(
            r"\[.*?\]\s*.*?\s+(.*?)\s+from\s+(.*?)\s+is joining\s+(.*?)(?:\.|$)",
            re.IGNORECASE
        )
        self.fetch_islands_task.start()

    async def cog_load(self):
        await init_db()

    def cog_unload(self):
        self.fetch_islands_task.cancel()

    @tasks.loop(hours=1)
    async def fetch_islands_task(self):
        await self.fetch_islands()

    @fetch_islands_task.before_loop
    async def before_fetch(self):
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

        if not island_clean:
            return island_name.title()

        # 1. Direct match
        if island_clean in self.island_map:
            channel_id = self.island_map[island_clean]
            return f"<#{channel_id}>"

        # 2. Flexible match (e.g. "Hiraya" vs "hiraya 1")
        for key, channel_id in self.island_map.items():
            if island_clean in key or key in island_clean:
                return f"<#{channel_id}>"

        return island_name.title()

    def split_options(self, raw: str):
        if not raw: return []
        parts = [p.strip() for p in raw.split("/") if p.strip()]
        return [self.clean_text(p) for p in parts if self.clean_text(p)]

    def parse_member_nick(self, display_name: str):
        if not display_name or "|" not in display_name: return [], []
        chunks = [c.strip() for c in display_name.split("|") if c.strip()]
        if not chunks: return [], []
        ign_opts    = self.split_options(chunks[0])
        island_opts = self.split_options(" | ".join(chunks[1:])) if len(chunks) > 1 else []
        return ign_opts, island_opts

    def find_matching_members(self, guild, ign_log, island_log):
        found_members    = []
        ign_log_clean    = self.clean_text(ign_log)
        island_log_clean = self.clean_text(island_log)

        for member in guild.members:
            ign_opts, island_opts = self.parse_member_nick(member.display_name)
            if not ign_opts and not island_opts: continue
            ign_match    = ign_log_clean in ign_opts
            island_match = island_log_clean in island_opts if island_opts else True
            if ign_match and island_match:
                found_members.append(member)
        return found_members

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author == self.bot.user or message.channel.id != Config.FLIGHT_LISTEN_CHANNEL_ID:
            return
        match = self.join_pattern.search(message.content)
        if match:
            ign_raw    = match.group(1).strip()
            island_raw = match.group(2).strip()
            dest_raw   = match.group(3).strip()
            found = self.find_matching_members(message.guild, ign_raw, island_raw)
            await self.log_result(found, "JOINING", ign_raw, island_raw, dest_raw)

    async def log_result(self, found_members, status, ign, island, destination):
        output_channel = self.bot.get_channel(Config.FLIGHT_LOG_CHANNEL_ID)
        if not output_channel: return

        if found_members:
            mentions = " ".join([m.mention for m in found_members])
            logger.info(f"[FLIGHT] ✅ Match: {ign} | {mentions}")
        else:
            destination_link = self.get_island_channel_link(destination)
            embed = discord.Embed(
                title=f"{Config.EMOJI_FAIL} UNKNOWN TRAVELER in {destination_link}",
                description=(
                    "**Identity Unknown:** Traveler is attempting to join but is not linked to a member.\n\n"
                    "**Select an action below to resolve.**"
                ),
                color=0xFF0000,
                timestamp=discord.utils.utcnow()
            )
            embed.add_field(name="👤 Traveler (IGN)", value=f"```yaml\n{ign}```", inline=True)
            embed.add_field(name="🏝️ Origin Island", value=f"```yaml\n{island.title()}```", inline=True)
            embed.set_image(url=Config.FOOTER_LINE)
            guild      = self.bot.get_guild(Config.GUILD_ID)
            guild_icon = guild.icon.url if guild and guild.icon else None
            embed.set_footer(text="Chopaeng Camp™", icon_url=guild_icon)

            view = TravelerActionView(self.bot, ign)
            await output_channel.send(embed=embed, view=view)

async def setup(bot):
    await bot.add_cog(FlightLoggerCog(bot))