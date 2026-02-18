"""
Discord Flight Logger Module
Tracks island visitor arrivals, alerts on unknown travelers, and handles moderation internally.
"""

import re
import logging
import unicodedata
import datetime
import asyncio
import aiosqlite  # Requires: pip install aiosqlite

import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord.ui import View, UserSelect, Select, button
from utils.config import Config
from utils.helpers import clean_text

logger = logging.getLogger("FlightLogger")

# --- CONSTANTS ---
# Colors
COLOR_SUCCESS = 0x2ECC71      # Green (for admits, unwarns)
COLOR_INVESTIGATION = 0xF1C40F  # Amber/Yellow (for investigation)
COLOR_WARN = 0xE67E22          # Orange (for warnings)
COLOR_KICK = 0xF1C40F          # Yellow (for kicks)
COLOR_BAN = 0x992D22           # Red (for bans)

# --- DATABASE SETUP ---
DB_NAME = "warnings.db"

# --- DATABASE HELPERS ---
async def init_db():
    """Initializes the database schema."""
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

DEFAULT_REASON_TEXT = (
    "Breaking [Sub Rule #2](https://discord.com/channels/729590421478703135/"
    "783677194576330792/1137904975553499217). We have removed your island access "
    "for now. Please read the <#783677194576330792> again to gain access."
)

REASON_TEMPLATES = {
    "rule_top_1": "Breaking [Sub Top Rule](https://discord.com/channels/729590421478703135/783677194576330792/1249835404098801756) or [Sub Rule #1](https://discord.com/channels/729590421478703135/783677194576330792/1249835467067752461). We have removed your island access for now.",
    "rule_2": "Breaking [Sub Rule #2](https://discord.com/channels/729590421478703135/783677194576330792/1137904975553499217). We have removed your island access for now. Please read the <#783677194576330792> again to gain access.",
    "rule_3_4": "Breaking [Sub Rule #3](https://discord.com/channels/729590421478703135/783677194576330792/1137905005433733211)/[Sub Rule #4](https://discord.com/channels/729590421478703135/783677194576330792/1137905033699151893). We have removed your island access for now.",
    "rule_6": "Breaking [Sub Rule #6](https://discord.com/channels/729590421478703135/783677194576330792/1137905106919096442). We have removed your island access for now. Please read the <#783677194576330792> again to gain access.",
    "rule_8": "Breaking [Sub Rule #8](https://discord.com/channels/729590421478703135/783677194576330792/1137905158257397875). We have removed your island access for now. Please read the <#783677194576330792> again to gain access."
}

REASON_OPTIONS = [
    discord.SelectOption(label="Sub Top Rule / Rule #1", value="rule_top_1", description="Breaking [Sub Top Rule]"),
    discord.SelectOption(label="Sub Rule #2", value="rule_2", description="Breaking [Sub Rule #2]"),
    discord.SelectOption(label="Sub Rule #3 / #4", value="rule_3_4", description="Breaking [Sub Rule #3]"),
    discord.SelectOption(label="Sub Rule #6", value="rule_6", description="Breaking [Sub Rule #6]"),
    discord.SelectOption(label="Sub Rule #8", value="rule_8", description="Breaking [Sub Rule #8]"),
    discord.SelectOption(label="Custom Reason", value="custom", description="Provide a custom reason"),
]

DURATION_OPTIONS = [
    discord.SelectOption(label="1 Hour",    value="1h"),
    discord.SelectOption(label="1 Day",     value="1d"),
    discord.SelectOption(label="2 Days",    value="2d"),
    discord.SelectOption(label="3 Days",    value="3d"),
    discord.SelectOption(label="1 Week",    value="1w"),
    discord.SelectOption(label="Permanent", value="perm"),
]

def _build_options_with_default(base_options: list[discord.SelectOption], selected_value: str | None, custom_text: str | None = None):
    new_options = []
    for opt in base_options:
        label = opt.label
        description = opt.description
        is_default = (opt.value == selected_value)

        if opt.value == "custom" and custom_text:
            cleaned_text = custom_text.replace("\n", " ").strip()
            display_text = (cleaned_text[:50] + "...") if len(cleaned_text) > 50 else cleaned_text
            label = f"Custom: {display_text}"
            description = "Click to modify your custom reason"

        new_options.append(
            discord.SelectOption(
                label=label, value=opt.value, description=description,
                default=is_default
            )
        )
    return new_options

def create_sapphire_log(member: discord.Member, mod: discord.Member, reason: str, case_id: str, warn_count: int, duration: str, action_verb: str):
    """Generates the visual embed mimicking Sapphire"""
    now = discord.utils.utcnow()
    # Sapphire typically expires in 7 days
    expiry = now + datetime.timedelta(days=7)
    expiry_ts = int(expiry.timestamp())
    
    mod_role_name = mod.top_role.name if hasattr(mod, 'top_role') and mod.top_role else "Moderator"

    if action_verb.upper() in ["KICKED", "BANNED"]:
        desc_lines = [
            f"> **{member.mention} ({member.display_name})** has been {action_verb.lower()}!",
            f"> **Reason:** {reason}",
            f"> **Responsible:** {mod.mention} ({mod_role_name})",
        ]
    else:
        desc_lines = [
            f"> **{member.mention} ({member.display_name})** has been {action_verb.lower()}!",
            f"> **Reason:** {reason}",
            f"> **Duration:** {duration}",
            f"> **Count:** {warn_count}",
            f"> **Responsible:** {mod.mention} ({mod_role_name})",
            f"> Automatically expires <t:{expiry_ts}:R>",
            f"> **Proof:** Verified (Log System)",
            "> ",
            "> **For Sub Members**: Please double check our <#783677194576330792> channel.",
            "> **For Free Members**: Kindly refer to our <#755522711492493342> channel."
        ]

    embed = discord.Embed(
        title=f"**{action_verb.title()} Case ID: {case_id}**",
        description="\n".join(desc_lines),
        color=0xff0000,
        timestamp=now
    )
    embed.set_thumbnail(url="https://i.ibb.co/HXyRH3R/2668-Siren.gif")
    embed.set_footer(text=f"Mod: {mod.display_name}", icon_url=mod.display_avatar.url)
    return embed

# --- UI VIEWS ---

# --- REFACTORED UI COMPONENTS ---

class TargetSelect(discord.ui.UserSelect):
    def __init__(self, parent_view):
        super().__init__(
            placeholder="1. Select the Target User...",
            min_values=1,
            max_values=1,
            row=0
        )
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        # discord.py 2.0+ automatically resolves members in self.values
        if self.values:
            # self.values[0] is typically a Member or User object
            self.parent_view.selected_member = self.values[0]
        
        await self.parent_view.refresh_state(interaction)

class DurationSelect(discord.ui.Select):
    def __init__(self, parent_view, current_duration):
        options = _build_options_with_default(DURATION_OPTIONS, current_duration)
        super().__init__(
            placeholder="2. Select Duration",
            min_values=1,
            max_values=1,
            options=options,
            row=1
        )
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        if self.values:
            self.parent_view.selected_duration = self.values[0]
        await self.parent_view.refresh_state(interaction)

class CustomReasonModal(discord.ui.Modal, title="Custom Punishment Reason"):
    reason_input = discord.ui.TextInput(
        label="Reason",
        placeholder="Enter the specific reason for this action...",
        style=discord.TextStyle.paragraph,
        required=True,
        min_length=5,
        max_length=500
    )

    def __init__(self, parent_view):
        super().__init__()
        self.parent_view = parent_view

    async def on_submit(self, interaction: discord.Interaction):
        self.parent_view.selected_reason = "custom"
        self.parent_view.custom_reason_text = self.reason_input.value
        await self.parent_view.refresh_state(interaction)

class ReasonSelect(discord.ui.Select):
    def __init__(self, parent_view, current_reason, custom_text=None):
        options = _build_options_with_default(REASON_OPTIONS, current_reason, custom_text)
        super().__init__(
            placeholder="3. Select Reason",
            min_values=1,
            max_values=1,
            options=options,
            row=2
        )
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        if self.values:
            selected = self.values[0]
            if selected == "custom":
                await interaction.response.send_modal(CustomReasonModal(self.parent_view))
            else:
                self.parent_view.selected_reason = selected
                self.parent_view.custom_reason_text = None
                await self.parent_view.refresh_state(interaction)

class ConfirmButton(discord.ui.Button):
    def __init__(self, parent_view, label, style, disabled):
        super().__init__(label=label, style=style, disabled=disabled, row=3)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        await self.parent_view.execute_punishment(interaction)

class CancelButton(discord.ui.Button):
    def __init__(self, parent_view):
        super().__init__(label="Cancel", style=discord.ButtonStyle.secondary, row=3)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content="Action cancelled.", view=None)
        self.parent_view.stop()

# --- REFACTORED BUILDER VIEW ---

class PunishmentBuilderView(discord.ui.View):
    def __init__(self, action_type: str, original_view: "TravelerActionView", log_message: discord.Message):
        super().__init__(timeout=3600)
        self.action_type = action_type
        self.original_view = original_view
        self.log_message = log_message

        self.selected_member: discord.Member | discord.User | None = None
        self.selected_duration: str | None = None
        self.selected_reason: str | None = None
        self.custom_reason_text: str | None = None
        
        # Initial render
        self._update_components()

    def _update_components(self):
        """Clear and re-add components based on current state."""
        self.clear_items()

        self.add_item(TargetSelect(self))

        if self.action_type == "WARN":
            self.add_item(DurationSelect(self, self.selected_duration))
        self.add_item(ReasonSelect(self, self.selected_reason, self.custom_reason_text))

        # 4. Confirm & Cancel Buttons
        # Submission restricted until all required fields are filled
        has_member = self.selected_member is not None
        has_reason = self.selected_reason is not None
        has_duration = self.selected_duration is not None or self.action_type != "WARN"

        can_submit = has_member and has_reason and has_duration
        
        if self.selected_member:
            target_name = getattr(self.selected_member, "display_name", str(self.selected_member))
            label = f"Confirm {self.action_type.title()} on {target_name}"
        else:
            label = "Confirm Action"

        style = discord.ButtonStyle.danger
        self.add_item(ConfirmButton(self, label, style, disabled=not can_submit))
        self.add_item(CancelButton(self))

    async def refresh_state(self, interaction: discord.Interaction):
        """Called by children to update the view state and message."""
        self._update_components()
        
        # Use edit_original_response if the interaction has already been responded to (e.g. Modal)
        if interaction.response.is_done():
            await interaction.edit_original_response(view=self)
        else:
            await interaction.response.edit_message(view=self)

    async def execute_punishment(self, interaction: discord.Interaction):
        """Pass execution to the Cog for cleaner logic."""
        cog = interaction.client.get_cog("FlightLoggerCog")
        if not cog:
            return await interaction.response.send_message("Error: FlightLoggerCog not found.", ephemeral=True)

        # Disable EVERYTHING in the builder view to prevent double-click
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)

        target = self.selected_member
        
        if self.selected_reason == "custom" and self.custom_reason_text:
            reason_text = self.custom_reason_text
        else:
            reason_text = REASON_TEMPLATES.get(self.selected_reason, DEFAULT_REASON_TEXT)
        
        await cog._execute_punishment_internal(
            interaction,
            target,
            self.action_type,
            reason_text,
            self.selected_duration,
            self.original_view,
            self.log_message
        )
        self.stop()


class AdmitConfirmView(discord.ui.View):
    """Confirmation dialog for admitting a traveler."""
    def __init__(self, parent_view: "TravelerActionView", ign: str, original_alert_message: discord.Message):
        super().__init__(timeout=300)
        self.parent_view = parent_view
        self.ign = ign
        self.original_alert_message = original_alert_message

    @discord.ui.button(label="Yes, Admit", style=discord.ButtonStyle.success)
    async def confirm_admit(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Proceed with admission."""
        msg = f"**{self.ign or 'Visitor'}** is cleared for entry."
        # Update the original alert message (the flight log alert)
        await self.parent_view._resolve_alert(
            interaction, "AUTHORIZED", COLOR_SUCCESS, msg, log_message=self.original_alert_message
        )
        # Update the confirmation message to show success
        await interaction.response.edit_message(content=msg, view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_admit(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Cancel admission."""
        await interaction.response.edit_message(content="Admission cancelled.", view=None)
        self.stop()

        
class TravelerActionView(discord.ui.View):
    def __init__(self, bot=None, ign=None):
        super().__init__(timeout=None)
        self.bot = bot
        self.ign = ign

    def _get_ign_from_embed(self, embed: discord.Embed):
        """Extracts IGN from the '👤 Traveler (IGN)' field in the alert embed."""
        if not embed or not embed.fields:
            return None
        for field in embed.fields:
            if "Traveler (IGN)" in field.name:
                # Value is usually "```yaml\nIGN```"
                match = re.search(r"```(?:yaml)?\n(.*?)\n?```", field.value)
                if match:
                    return match.group(1).strip()
        return None

    async def _resolve_alert(self, interaction, status_label, color, log_msg, target_user=None, log_message=None):
        """Internal helper to update the alert embed state. Does NOT send interaction responses."""
        target_str      = f"{target_user.mention}" if target_user else "Visitor (unlinked)"
        message_to_edit = log_message or (interaction.message if interaction.response.is_done() else None)

        if not message_to_edit:
            return

        try:
            # Refresh message state if possible to avoid 404
            embed = message_to_edit.embeds[0]
            
            # Remove investigation field if it exists (case was under investigation)
            fields_to_keep = [f for f in embed.fields if "🔍 Investigating" not in f.name]
            embed.clear_fields()
            for field in fields_to_keep:
                embed.add_field(name=field.name, value=field.value, inline=field.inline)
            
            # Update color and header
            embed.color = color
            embed.set_author(name=f"CASE CLOSED: {status_label}", icon_url=interaction.user.display_avatar.url)
            embed.add_field(
                name="<:ChoLove:818216528449241128> Action Taken",
                value=f"**{status_label}** by {interaction.user.mention}\nTarget: {target_str}",
                inline=False
            )
            self.disable_all_items()
            await message_to_edit.edit(embed=embed, view=self)
        except Exception as e:
            logger.error(f"Error editing original message: {e}")

    def disable_all_items(self):
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="Admit", style=discord.ButtonStyle.success, emoji="<:Cho_Check:1456715827213504593>", custom_id="fl_admit", row=0)
    async def confirm_action(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.NotFound:
            return  # Stale interaction, silently ignore
        ign = self.ign or self._get_ign_from_embed(interaction.message.embeds[0])
        # Show confirmation dialog
        confirm_view = AdmitConfirmView(self, ign, interaction.message)
        await interaction.followup.send(
            f"Are you sure you want to admit **{ign or 'Visitor'}**?",
            view=confirm_view,
            ephemeral=True
        )

    @discord.ui.button(label="Investigate", style=discord.ButtonStyle.secondary, emoji="<:AmongUs_Investigate:784046584299257857>", custom_id="fl_investigate", row=0)
    async def investigate_action(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.NotFound:
            return  # Stale interaction, silently ignore
        
        ign = self.ign or self._get_ign_from_embed(interaction.message.embeds[0])
        mod = interaction.user
        timestamp = int(discord.utils.utcnow().timestamp())
        
        try:
            message_to_edit = interaction.message
            embed = message_to_edit.embeds[0]
            
            # Update color to amber/yellow
            embed.color = COLOR_INVESTIGATION
            
            # Update author to show investigation status
            embed.set_author(name="🔍 UNDER INVESTIGATION", icon_url=mod.display_avatar.url)
            
            # Add investigation field
            embed.add_field(
                name="🔍 Investigating",
                value=f"**{mod.mention}** is looking into this. Started <t:{timestamp}:R>",
                inline=False
            )
            
            # Disable only the Investigate button
            button.disabled = True
            
            await message_to_edit.edit(embed=embed, view=self)
            await interaction.followup.send("🔍 Marked as under investigation.", ephemeral=True)
        except Exception as e:
            logger.error(f"Error marking as under investigation: {e}")
            await interaction.followup.send(f"Error: {e}", ephemeral=True)

    @discord.ui.button(label="Warn", style=discord.ButtonStyle.primary, emoji="<:Cho_Warn:1456712416271405188>", custom_id="fl_warn", row=1)
    async def warn_action(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.NotFound:
            return
        view = PunishmentBuilderView("WARN", self, log_message=interaction.message)
        await interaction.followup.send("<:Cho_Warn:1456712416271405188> **Build Warning:**", view=view, ephemeral=True)

    @discord.ui.button(label="Kick", style=discord.ButtonStyle.secondary, emoji="<:Cho_Kick:1456714701630214349>", custom_id="fl_kick", row=1)
    async def kick_action(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.NotFound:
            return
        view = PunishmentBuilderView("KICK", self, log_message=interaction.message)
        await interaction.followup.send("<:Cho_Kick:1456714701630214349> **Build Kick:**", view=view, ephemeral=True)

    @discord.ui.button(label="Ban", style=discord.ButtonStyle.danger, emoji="<:Cho_Ban:1473530840725061793>", custom_id="fl_ban", row=1)
    async def ban_action(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.NotFound:
            return
        view = PunishmentBuilderView("BAN", self, log_message=interaction.message)
        await interaction.followup.send("<:Cho_Ban:1473530840725061793> **Build Ban:**", view=view, ephemeral=True)
    
class FlightLoggerCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.island_map = {}
        self.join_pattern = re.compile(
            r"\[.*?\]\s*.*?\s+(.*?)\s+from\s+(.*?)\s+is joining\s+(.*?)(?:\.|$)",
            re.IGNORECASE
        )
        self._db_conn = None
        self.last_processed = None
        self.fetch_islands_task.start()

    async def _get_db(self):
        if self._db_conn is None:
            self._db_conn = await aiosqlite.connect(DB_NAME)
        return self._db_conn

    async def add_warning(self, user_id, guild_id, reason, mod_id):
        db = await self._get_db()
        await db.execute("INSERT INTO warnings VALUES (?, ?, ?, ?, ?)",
                         (user_id, guild_id, reason, mod_id, int(discord.utils.utcnow().timestamp())))
        await db.commit()

    async def get_warn_count(self, user_id: int, guild_id: int, days: int = 3):
        db = await self._get_db()
        cutoff = int((discord.utils.utcnow() - datetime.timedelta(days=days)).timestamp())
        cursor = await db.execute(
            "SELECT COUNT(*) FROM warnings WHERE user_id = ? AND guild_id = ? AND timestamp > ?",
            (user_id, guild_id, cutoff)
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def remove_latest_warning(self, user_id: int, guild_id: int):
        """Remove the most recent warning for a user and return its details."""
        db = await self._get_db()
        cursor = await db.execute(
            "SELECT rowid, reason, mod_id, timestamp FROM warnings WHERE user_id = ? AND guild_id = ? ORDER BY timestamp DESC LIMIT 1",
            (user_id, guild_id)
        )
        row = await cursor.fetchone()
        if row:
            rowid, reason, mod_id, timestamp = row
            await db.execute("DELETE FROM warnings WHERE rowid = ?", (rowid,))
            await db.commit()
            return {"reason": reason, "mod_id": mod_id, "timestamp": timestamp}
        return None

    async def get_warnings(self, user_id: int, guild_id: int, days: int = 30):
        """Get all warnings for a user within the specified number of days."""
        db = await self._get_db()
        cutoff = int((discord.utils.utcnow() - datetime.timedelta(days=days)).timestamp())
        cursor = await db.execute(
            "SELECT reason, mod_id, timestamp FROM warnings WHERE user_id = ? AND guild_id = ? AND timestamp > ? ORDER BY timestamp DESC",
            (user_id, guild_id, cutoff)
        )
        rows = await cursor.fetchall()
        return [{"reason": r[0], "mod_id": r[1], "timestamp": r[2]} for r in rows]

    async def _execute_punishment_internal(self, interaction, target, action_type, reason_text, duration_str, original_view, log_message):
        """Unified internal method for handling moderation actions."""
        mod = interaction.user
        guild = interaction.guild
        
        # 1. Determine action details
        if action_type == "BAN":
            final_duration = "Permanent"
            action_verb = "BANNED"
            color = COLOR_BAN
        elif action_type == "KICK":
            final_duration = "N/A"
            action_verb = "KICKED"
            color = COLOR_KICK
        else: # WARN
            final_duration = duration_str
            action_verb = "WARNED"
            color = COLOR_WARN

        # Generate unique case ID: FL- YYMM-RAND
        now = discord.utils.utcnow()
        case_id = f"FL-{now.strftime('%y%m')}-{hex(int(now.timestamp()))[2:][-4:].upper()}"

        # 1.5 Role Removal (Warn Only)
        if action_type == "WARN":
            visitor_role = guild.get_role(Config.ISLAND_ACCESS_ROLE)
            if visitor_role and visitor_role in target.roles:
                try:
                    await target.remove_roles(visitor_role, reason=f"FlightLog [{case_id}]: Warned - Role Removed")
                    logger.info(f"[FLIGHT] Removed role {visitor_role.name} from {target.display_name}")
                except discord.Forbidden:
                    logger.error(f"[FLIGHT] Permission Denied: Cannot remove role from {target.display_name}")
                except Exception as e:
                    logger.error(f"[FLIGHT] Error removing role: {e}")

        try:
            # 2. DM Notification
            try:
                emoji = ""
                if action_type == "BAN": emoji = "<:Cho_Ban:1473530840725061793> "
                elif action_type == "KICK": emoji = "<:Cho_Kick:1456714701630214349> "
                elif action_type == "WARN": emoji = "<:Cho_Warn:1456712416271405188> "

                dm_embed = discord.Embed(
                    title=f"{emoji} Chobot Notification",
                    description=f"You have been **{action_verb.lower()}** from **{guild.name}**.",
                    color=color,
                    timestamp=discord.utils.utcnow()
                )
                dm_embed.add_field(name="Reason", value=reason_text, inline=False)
                dm_embed.set_footer(text=f"Case ID: {case_id}")
                if guild.icon:
                    dm_embed.set_thumbnail(url=guild.icon.url)

                await target.send(embed=dm_embed)
            except discord.HTTPException:
                pass # DM Closed

            # 3. Discord Action
            if action_type == "KICK":
                await target.kick(reason=f"FlightLog [{case_id}]: {reason_text}")
            elif action_type == "BAN":
                await target.ban(reason=f"FlightLog [{case_id}]: {reason_text}")

            # 4. Database Log
            await self.add_warning(target.id, guild.id, reason_text, mod.id)
            # Use small delay to ensure DB consistency (though commit is awaited)
            new_count = await self.get_warn_count(target.id, guild.id, days=3)

            # 5. Log to Sapphire Channel
            log_embed = create_sapphire_log(target, mod, reason_text, case_id, new_count, final_duration, action_verb)
            sub_mod_channel = guild.get_channel(Config.SUB_MOD_CHANNEL_ID)
            
            if sub_mod_channel:
                await sub_mod_channel.send(content=target.mention, embed=log_embed)
                await interaction.followup.send(f"✅ Case `{case_id}` logged in {sub_mod_channel.mention}", ephemeral=True)
            else:
                await interaction.followup.send(f"✅ Action executed (Case `{case_id}`), but log channel is missing.", ephemeral=True)

            # 6. Update Original Alert
            msg_to_mod = f"✅ **{target.display_name}** processed ({action_verb}). Case: `{case_id}`"
            if original_view:
                await original_view._resolve_alert(
                    interaction, action_verb, color, msg_to_mod,
                    target_user=target, log_message=log_message
                )

        except discord.Forbidden:
            await interaction.followup.send("Permission Denied. Check bot role hierarchy.", ephemeral=True)
        except Exception as e:
            logger.error(f"Punishment Error: {e}")
            await interaction.followup.send(f"System Error: {e}", ephemeral=True)

    async def cog_load(self):
        await init_db()
        self.bot.add_view(TravelerActionView(bot=self.bot))

    def cog_unload(self):
        self.fetch_islands_task.cancel()
        if self._db_conn:
            asyncio.create_task(self._db_conn.close())

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

            # e.g. "🌴┆bituin" -> "bituin"
            chan_clean = clean_text(channel.name)
            if chan_clean:
                temp_map[chan_clean] = channel.id
                count += 1
            
            all_possible_islands = Config.SUB_ISLANDS
            for island in all_possible_islands:
                island_clean = clean_text(island)
                if island_clean and island_clean in chan_clean:
                    temp_map[island_clean] = channel.id

        self.island_map = temp_map
        logger.info(f"[FLIGHT] Dynamic Island Fetch Complete. Mapped {len(temp_map)} keys.")

    def get_island_channel_link(self, island_name):
        """Get channel link with robust fallback search"""
        island_clean = clean_text(island_name)
        if not island_clean:
            return island_name.title()

        if island_clean in self.island_map:
            return f"<#{self.island_map[island_clean]}>"

        for key, channel_id in self.island_map.items():
            if island_clean == key:
                return f"<#{channel_id}>"
            if island_clean in key:
                return f"<#{channel_id}>"
        guild = self.bot.get_guild(Config.GUILD_ID)
        if guild:
            for channel in guild.text_channels:
                chan_clean = clean_text(channel.name)
                if island_clean == chan_clean or island_clean in chan_clean:
                    self.island_map[island_clean] = channel.id
                    return channel.mention

        return island_name.title()
    def split_options(self, raw: str):
        if not raw: return []
        parts = [p.strip() for p in raw.split("/") if p.strip()]
        return [clean_text(p) for p in parts if clean_text(p)]

    def parse_member_nick(self, display_name: str):
        if not display_name or "|" not in display_name: return [], []
        chunks = [c.strip() for c in display_name.split("|") if c.strip()]
        if not chunks: return [], []
        ign_opts    = self.split_options(chunks[0])
        island_opts = self.split_options(" | ".join(chunks[1:])) if len(chunks) > 1 else []
        return ign_opts, island_opts

    def find_matching_members(self, guild, ign_log_clean, island_log_clean):
        found_members = []
        for member in guild.members:
            ign_opts, island_opts = self.parse_member_nick(member.display_name)
            if not ign_opts and not island_opts: continue
            
            ign_match = ign_log_clean in ign_opts
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
            ign_clean = clean_text(ign_raw)
            isl_clean = clean_text(island_raw)
            found = await asyncio.to_thread(self.find_matching_members, message.guild, ign_clean, isl_clean)
            await self.log_result(found, "JOINING", ign_raw, island_raw, dest_raw)

    async def log_result(self, found_members, status, ign, island, destination):
        output_channel = self.bot.get_channel(Config.FLIGHT_LOG_CHANNEL_ID)
        if not output_channel: return

        if found_members:
            mentions = " ".join([m.mention for m in found_members])
            logger.info(f"[FLIGHT] Match: {ign} | {mentions}")
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

    @commands.command(name="recoverflights")
    @commands.has_permissions(administrator=True)
    async def recover_flights(self, ctx, hours: int = 48, mode: str = "dry"):
        """
        Scrapes past logs chronologically (Oldest -> Newest).
        Usage: !recoverflights [hours_back] [dry/run]
        """
        listen_channel = self.bot.get_channel(Config.FLIGHT_LISTEN_CHANNEL_ID)
        if not listen_channel:
            return await ctx.send(f"[ERR] Listener channel {Config.FLIGHT_LISTEN_CHANNEL_ID} not found.")

        dry_run = mode.lower() != "run"
        status_header = f"Scanning history for the last **{hours} hours**..."
        status_mode = "DRY RUN" if dry_run else "LIVE EXECUTION"
        status_msg = await ctx.send(f"**{status_header}**\nMode: {status_mode}")

        cutoff = discord.utils.utcnow() - datetime.timedelta(hours=hours)
        found_count = 0
        processed_count = 0

        # oldest_first=True ensures logs are posted in the order they happened (Past -> Present)
        async for message in listen_channel.history(after=cutoff, limit=None, oldest_first=True):
            if message.author == self.bot.user:
                continue

            match = self.join_pattern.search(message.content)
            if match:
                found_count += 1

                if not dry_run:
                    try:
                        ign_raw    = match.group(1).strip()
                        island_raw = match.group(2).strip()
                        dest_raw   = match.group(3).strip()

                        ign_clean = clean_text(ign_raw)
                        isl_clean = clean_text(island_raw)

                        found = await asyncio.to_thread(self.find_matching_members, message.guild, ign_clean, isl_clean)

                        # Trigger the log result
                        await self.log_result(found, "JOINING", ign_raw, island_raw, dest_raw)
                        logger.info(f"[RECOVER] Processed item #{processed_count} - {ign_raw}")

                        processed_count += 1
                        await asyncio.sleep(1.5)
                    except Exception as e:
                        logger.error(f"[RECOVER] Failed to process message {message.id}: {e}")

        if dry_run:
            await status_msg.edit(content=f"**Scan Complete (Dry Run)**\nFound: {found_count} matches.\n\nCommand to execute:\n`!recoverflights {hours} run`")
        else:
            await status_msg.edit(content=f"**Recovery Complete**\nProcessed: {processed_count} flights.")

    @commands.command(name="flightstatus", aliases=["fstatus"])
    @commands.has_permissions(manage_messages=True)
    async def flight_status(self, ctx):
        """Diagnose connection, channels, and last activity."""

        listen_chan = self.bot.get_channel(Config.FLIGHT_LISTEN_CHANNEL_ID)
        log_chan = self.bot.get_channel(Config.FLIGHT_LOG_CHANNEL_ID)

        lines = []

        # Listener Status
        if listen_chan:
            perms = listen_chan.permissions_for(ctx.guild.me)
            if perms.read_messages:
                lines.append(f"[OK] Listener Channel: {listen_chan.name}")
            else:
                lines.append(f"[WARN] Listener Channel: {listen_chan.name} (No Read Access)")
        else:
            lines.append(f"[ERR] Listener Channel: Missing (ID: {Config.FLIGHT_LISTEN_CHANNEL_ID})")

        # Log Output Status
        if log_chan:
            perms = log_chan.permissions_for(ctx.guild.me)
            if perms.send_messages:
                lines.append(f"[OK] Log Channel: {log_chan.name}")
            else:
                lines.append(f"[WARN] Log Channel: {log_chan.name} (No Send Access)")
        else:
            lines.append(f"[ERR] Log Channel: Missing (ID: {Config.FLIGHT_LOG_CHANNEL_ID})")

        # Database Status
        if self._db_conn:
            lines.append("[OK] Database: Connected")
        else:
            lines.append("[WARN] Database: Disconnected (Connects on write)")

        # Last Activity
        if self.last_processed:
            ts = int(self.last_processed.timestamp())
            lines.append(f"[INFO] Last Flight: <t:{ts}:R>")
        else:
            lines.append("[INFO] Last Flight: None since restart")

        embed = discord.Embed(
            title="System Status",
            description="```ini\n" + "\n".join(lines) + "\n```",
            color=0x2b2d31  # Dark/Neutral
        )
        await ctx.send(embed=embed)

    @commands.command(name="flightdebug", aliases=["fdebug"])
    @commands.has_permissions(manage_messages=True)
    async def flight_debug(self, ctx, *, test_string: str = None):
        """
        Test the regex against a raw message string.
        Usage: !fdebug [Dodo Code Message]
        """
        if not test_string:
            return await ctx.send("**Usage:** `!fdebug [Message Content]`")

        match = self.join_pattern.search(test_string)

        if match:
            ign = match.group(1).strip()
            island = match.group(2).strip()
            dest = match.group(3).strip()

            embed = discord.Embed(title="Regex Match Successful", color=0x2b2d31)
            embed.add_field(name="IGN", value=f"`{ign}`")
            embed.add_field(name="Island", value=f"`{island}`")
            embed.add_field(name="Destination", value=f"`{dest}`")
            await ctx.send(embed=embed)
        else:
            embed = discord.Embed(title="Regex Match Failed", color=0xff0000)
            embed.description = (
                "Input did not match pattern.\n"
                "**Check:** Format changes, hidden characters, or case sensitivity."
            )
            embed.add_field(name="Current Pattern", value=f"```regex\n{self.join_pattern.pattern}\n```", inline=False)
            await ctx.send(embed=embed)

    @app_commands.command(name="unwarn", description="Remove the most recent warning from a user")
    @app_commands.describe(user="The user to unwarn", reason="Reason for removing the warning (optional)")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def unwarn_slash(self, interaction: discord.Interaction, user: discord.Member, reason: str = None):
        """Remove the most recent warning from a user (slash command)."""
        await self._unwarn_internal(interaction, user, reason, is_slash=True)

    @commands.command(name="unwarn", aliases=["removewarn"])
    @commands.has_permissions(manage_messages=True)
    async def unwarn_prefix(self, ctx, member: discord.Member, *, reason: str = None):
        """Remove the most recent warning from a user (prefix command)."""
        await self._unwarn_internal(ctx, member, reason, is_slash=False)

    async def _unwarn_internal(self, ctx_or_interaction, user: discord.Member, reason: str = None, is_slash: bool = True):
        """Internal method for unwarn logic."""
        # Handle both slash and prefix commands
        if is_slash:
            await ctx_or_interaction.response.defer(ephemeral=True)
            guild = ctx_or_interaction.guild
            mod = ctx_or_interaction.user
        else:
            guild = ctx_or_interaction.guild
            mod = ctx_or_interaction.author

        reason = reason or "No reason provided"

        # Remove the latest warning
        removed_warning = await self.remove_latest_warning(user.id, guild.id)
        
        if not removed_warning:
            msg = f"⚠️ **{user.display_name}** has no warnings to remove."
            if is_slash:
                await ctx_or_interaction.followup.send(msg, ephemeral=True)
            else:
                await ctx_or_interaction.send(msg)
            return

        # Get new warning count
        new_count = await self.get_warn_count(user.id, guild.id, days=3)

        # Restore island access role if user doesn't have it
        visitor_role = guild.get_role(Config.ISLAND_ACCESS_ROLE)
        if visitor_role and visitor_role not in user.roles:
            try:
                await user.add_roles(visitor_role, reason=f"Unwarn: {reason}")
                logger.info(f"[FLIGHT] Restored role {visitor_role.name} to {user.display_name}")
            except discord.Forbidden:
                logger.error(f"[FLIGHT] Permission Denied: Cannot add role to {user.display_name}")
            except Exception as e:
                logger.error(f"[FLIGHT] Error adding role: {e}")

        # Generate case ID
        now = discord.utils.utcnow()
        case_id = f"FL-{now.strftime('%y%m')}-{hex(int(now.timestamp()))[2:][-4:].upper()}"

        # DM the user
        try:
            dm_embed = discord.Embed(
                title="<:Cho_Check:1456715827213504593> Chobot Notification",
                description=f"A warning has been removed from your account in **{guild.name}**.",
                color=COLOR_SUCCESS,
                timestamp=discord.utils.utcnow()
            )
            dm_embed.add_field(name="Reason for Removal", value=reason, inline=False)
            dm_embed.set_footer(text=f"Case ID: {case_id}")
            if guild.icon:
                dm_embed.set_thumbnail(url=guild.icon.url)
            
            await user.send(embed=dm_embed)
        except discord.HTTPException:
            pass  # DM closed

        # Log to sub-mod channel (green embed similar to Sapphire style)
        log_embed = self._create_unwarn_log(user, mod, reason, case_id, new_count)
        sub_mod_channel = guild.get_channel(Config.SUB_MOD_CHANNEL_ID)
        
        if sub_mod_channel:
            await sub_mod_channel.send(content=user.mention, embed=log_embed)
            msg = f"✅ Case `{case_id}` logged in {sub_mod_channel.mention}"
        else:
            msg = f"✅ Warning removed (Case `{case_id}`), but log channel is missing."

        if is_slash:
            await ctx_or_interaction.followup.send(msg, ephemeral=True)
        else:
            await ctx_or_interaction.send(msg)

    def _create_unwarn_log(self, member: discord.Member, mod: discord.Member, reason: str, case_id: str, warn_count: int):
        """Creates a green log embed for unwarn action."""
        now = discord.utils.utcnow()
        mod_role_name = mod.top_role.name if hasattr(mod, 'top_role') and mod.top_role else "Moderator"
        
        desc_lines = [
            f"> **{member.mention} ({member.display_name})** has been unwarned!",
            f"> **Reason:** {reason}",
            f"> **New Count:** {warn_count}",
            f"> **Responsible:** {mod.mention} ({mod_role_name})",
        ]
        
        embed = discord.Embed(
            title=f"**Unwarned Case ID: {case_id}**",
            description="\n".join(desc_lines),
            color=COLOR_SUCCESS,
            timestamp=now
        )
        embed.set_thumbnail(url="https://i.ibb.co/HXyRH3R/2668-Siren.gif")
        embed.set_footer(text=f"Mod: {mod.display_name}", icon_url=mod.display_avatar.url)
        return embed

    @app_commands.command(name="warnings", description="List recent warnings for a user")
    @app_commands.describe(user="The user to check", days="Number of days to look back (default: 30)")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def warnings_slash(self, interaction: discord.Interaction, user: discord.Member, days: int = 30):
        """List warnings for a user (slash command)."""
        await self._warnings_internal(interaction, user, days, is_slash=True)

    @commands.command(name="warnings", aliases=["warnlist"])
    @commands.has_permissions(manage_messages=True)
    async def warnings_prefix(self, ctx, member: discord.Member, days: int = 30):
        """List warnings for a user (prefix command)."""
        await self._warnings_internal(ctx, member, days, is_slash=False)

    async def _warnings_internal(self, ctx_or_interaction, user: discord.Member, days: int = 30, is_slash: bool = True):
        """Internal method for listing warnings."""
        # Handle both slash and prefix commands
        if is_slash:
            await ctx_or_interaction.response.defer(ephemeral=True)
            guild = ctx_or_interaction.guild
        else:
            guild = ctx_or_interaction.guild

        # Get warnings
        warnings = await self.get_warnings(user.id, guild.id, days)
        
        if not warnings:
            msg = f"✅ **{user.display_name}** has no warnings in the last {days} days."
            if is_slash:
                await ctx_or_interaction.followup.send(msg, ephemeral=True)
            else:
                await ctx_or_interaction.send(msg)
            return

        # Build embed
        embed = discord.Embed(
            title=f"Warnings for {user.display_name}",
            description=f"Showing warnings from the last {days} days",
            color=COLOR_WARN,
            timestamp=discord.utils.utcnow()
        )
        embed.set_thumbnail(url=user.display_avatar.url)

        for i, warn in enumerate(warnings, 1):
            mod_id = warn['mod_id']
            mod = guild.get_member(mod_id)
            mod_text = mod.mention if mod else f"ID: {mod_id}"
            
            timestamp = warn['timestamp']
            reason = warn['reason']
            
            embed.add_field(
                name=f"#{i} - <t:{timestamp}:R>",
                value=f"**Moderator:** {mod_text}\n**Reason:** {reason}",
                inline=False
            )

        if is_slash:
            await ctx_or_interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await ctx_or_interaction.send(embed=embed)

async def setup(bot):
    await bot.add_cog(FlightLoggerCog(bot))
