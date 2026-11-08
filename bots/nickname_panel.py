"""Interactive nickname panel: members set a "Character | Island" nickname through a form.

The bot builds the nickname itself, so it is always in the format the flight logger
parses, and checks it against the member's identity limit before saving.

- /nickpanel deploys the panel. Its buttons are persistent (fixed custom_ids, no timeout)
  and keep working after restarts once register_nickname_commands() has run.
- Members with characters saved on the website can pick them instead of typing.
- Over the identity limit, the preview offers a "keep which" picker instead of a dead end.
- Mods get a "Fix Nickname" member context menu that runs the same flow for that member.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import discord
from discord import app_commands

from utils import island_access
from utils.flight_matching import calculate_max_identities, get_allowed_identity_count, parse_member_nick
from utils.nickname_format import (
    DISCORD_NICK_MAX,
    build_nickname,
    flatten_identities,
    is_valid_acnh_nickname,
    nickname_to_pairs,
    pairs_to_form_values,
    parse_identity_lines,
)

logger = logging.getLogger("NicknamePanel")

COLOR_PANEL = 0x7DD3C0
COLOR_OK = 0x57F287
COLOR_WARN = 0xFEE75C
COLOR_BAD = 0xED4245

_TIER_ROLE_IDS = {int(r) for r in island_access.ROLE_ID_TO_TIER_NAME if str(r).isdigit()}


# ---------------------------------------------------------------------------
# Member checks (same rules the flight logger flags on)
# ---------------------------------------------------------------------------

def member_subscription_roles(member: discord.Member, client=None) -> list[discord.Role]:
    """Subscription roles, from the flight logger's live channel scan when it is loaded."""
    cog = client.get_cog("FlightLoggerCog") if client is not None and hasattr(client, "get_cog") else None
    if cog is not None and hasattr(cog, "member_sub_roles"):
        return cog.member_sub_roles(member)
    return [r for r in member.roles if r.id in _TIER_ROLE_IDS]


def is_staff(member: discord.Member) -> bool:
    perms = getattr(member, "guild_permissions", None)
    if perms and perms.administrator:
        return True
    return island_access.is_mod([str(r.id) for r in getattr(member, "roles", [])])


def identity_report(member: discord.Member, nickname: str | None, client=None) -> dict:
    """How the flight logger reads `nickname` for this member, and whether it would be flagged."""
    igns, islands = parse_member_nick(nickname or "")
    subs = member_subscription_roles(member, client)
    identities = calculate_max_identities(nickname or "")
    allowed = get_allowed_identity_count(getattr(member, "joined_at", None), len(subs))
    staff = is_staff(member)
    valid = is_valid_acnh_nickname(nickname)
    return {
        "nickname": nickname or "",
        "igns": igns,
        "islands": islands,
        "subs": subs,
        "identities": identities,
        "allowed": allowed,
        "staff": staff,
        "valid": valid,
        "over_limit": valid and not staff and identities > allowed,
    }


def _report_lines(report: dict) -> list[str]:
    subs = ", ".join(r.mention for r in report["subs"]) or "None"
    limit = "no limit (staff)" if report["staff"] else str(report["allowed"])
    status = "✅" if not report["over_limit"] else "⚠️"
    return [
        f"**Characters:** {', '.join(report['igns']) or '—'}",
        f"**Islands:** {', '.join(report['islands']) or '—'}",
        f"**Identities:** {report['identities']} · **Allowed:** {limit} {status}",
        f"**Subscriptions:** {subs}",
    ]


def _limit_message(report: dict) -> str:
    extra = report["identities"] - report["allowed"]
    return (
        f"This nickname has **{report['identities']}** identities but only **{report['allowed']}** are allowed "
        f"(one per subscription). Remove {extra} identit{'ies' if extra != 1 else 'y'}, "
        "or add another subscription to keep them all."
    )


# ---------------------------------------------------------------------------
# Panel message
# ---------------------------------------------------------------------------

def build_panel_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🏝️ Set Your Camp Nickname",
        description=(
            "Your nickname is how the flight logger recognises you when you fly to a sub island.\n\n"
            "**Format:** `Character | Island`\n"
            "**Example:** `ChoPaeng | ChoPaeng Camp`\n\n"
            "✏️ **Set Nickname**: pick your characters saved on the website, or type them, and we'll "
            "format it for you.\n"
            "🔍 **Check My Nickname**: see how the bot reads your current nickname.\n"
            "❓ **Rules**: how many characters and islands you can list."
        ),
        color=COLOR_PANEL,
    )
    embed.set_footer(text="Chopaeng Camp™ • Nickname Panel")
    return embed


RULES_TEXT = (
    "**Nickname rules**\n"
    "• Format: `Character | Island`, using your in-game names.\n"
    "• Each character/island you list is one **identity**. You may list one identity per "
    "subscription. Members who joined before Sept 2022 may keep two.\n"
    "• Several islands for one character: `Character | Island1/Island2`.\n"
    "• Several characters: `Char1 | Island1 | Char2 | Island2`.\n"
    f"• Discord limits nicknames to {DISCORD_NICK_MAX} characters.\n"
    "• Listing more identities than your subscriptions allow gets your flights flagged for review."
)


def _member_or_none(interaction: discord.Interaction) -> discord.Member | None:
    user = interaction.user
    return user if isinstance(user, discord.Member) else None


# ---------------------------------------------------------------------------
# Saved characters (website profile)
# ---------------------------------------------------------------------------

def load_saved_characters(discord_id: int | str) -> list[tuple[str, str]]:
    """(IGN, island) identities the member saved on the website profile, default first."""
    uid = str(discord_id)
    try:
        from utils.unified_auth import resolve_all_user_ids
        cand_ids = list(dict.fromkeys([uid, *(resolve_all_user_ids({"user_id": uid, "discord_id": uid}) or [])]))
    except Exception:
        cand_ids = [uid]
    try:
        from utils.database import connect_db
        with connect_db() as conn:
            ph = ",".join("?" for _ in cand_ids)
            rows = conn.execute(
                f"SELECT ign, island_name FROM user_saved_characters WHERE user_id IN ({ph}) AND TRIM(ign) != '' "
                "ORDER BY is_default DESC, updated_at DESC LIMIT 50",
                cand_ids,
            ).fetchall()
    except Exception as exc:
        logger.warning(f"[NICK] Could not load saved characters for {uid}: {exc}")
        return []
    pairs = [(str(r["ign"] or "").strip(), str(r["island_name"] or "").strip()) for r in rows]
    return flatten_identities([(c, i) for c, i in pairs if c and i])


# ---------------------------------------------------------------------------
# Flow: member (or a mod fixing a member) -> saved picker / form -> preview -> confirm
# ---------------------------------------------------------------------------

@dataclass
class NickSession:
    target_id: int   # whose nickname changes
    actor_id: int    # who is pressing the buttons (the member, or a mod)

    @property
    def for_other(self) -> bool:
        return self.target_id != self.actor_id


async def _resolve_target(interaction: discord.Interaction, session: NickSession) -> discord.Member | None:
    guild = interaction.guild
    if guild is None:
        return None
    member = guild.get_member(session.target_id)
    if member is None:
        try:
            member = await guild.fetch_member(session.target_id)
        except discord.HTTPException:
            return None
    return member


def _not_yours(interaction: discord.Interaction, session: NickSession) -> bool:
    return interaction.user.id != session.actor_id


def build_preview(target: discord.Member, session: NickSession, pairs: list[tuple[str, str]], client):
    """Preview embed + view for these identities. Over the limit -> keep-which picker, no Confirm."""
    values = pairs_to_form_values(pairs)
    nickname, errors = build_nickname(pairs)
    if errors:
        embed = discord.Embed(
            title="Couldn't build the nickname",
            description="\n".join(f"• {e}" for e in errors),
            color=COLOR_BAD,
        )
        return embed, PreviewView(session, None, pairs, values)

    report = identity_report(target, nickname, client)
    over = report["over_limit"]
    who = f"For {target.mention}\n" if session.for_other else ""
    embed = discord.Embed(
        title="Preview",
        description=(
            f"{who}**New nickname:** `{nickname}` ({len(nickname)}/{DISCORD_NICK_MAX})\n\n"
            + "\n".join(_report_lines(report))
        ),
        color=COLOR_BAD if over else COLOR_OK,
    )
    if over:
        embed.add_field(
            name="Too many identities",
            value=_limit_message(report) + "\nPick which to keep below, or press **Edit**.",
            inline=False,
        )
    return embed, PreviewView(session, None if over else nickname, pairs, values, keep_limit=report["allowed"] if over else 0)


class NicknameModal(discord.ui.Modal, title="Set Nickname"):
    def __init__(self, session: NickSession, character: str = "", island: str = "", extras: str = ""):
        super().__init__(timeout=600)
        self.session = session
        self.character = discord.ui.TextInput(
            label="Character name", placeholder="In-game name", default=character[:32] or None,
            max_length=32, required=True,
        )
        self.island = discord.ui.TextInput(
            label="Island name", placeholder="Island1 or Island1/Island2", default=island[:64] or None,
            max_length=64, required=True,
        )
        self.extras = discord.ui.TextInput(
            label="More characters (optional)",
            style=discord.TextStyle.paragraph,
            placeholder="One per line, e.g.\nChar2 | Island2",
            default=extras[:300] or None,
            max_length=300,
            required=False,
        )
        self.add_item(self.character)
        self.add_item(self.island)
        self.add_item(self.extras)

    async def on_submit(self, interaction: discord.Interaction):
        target = await _resolve_target(interaction, self.session)
        if target is None:
            await interaction.response.send_message("That member isn't in the server anymore.", ephemeral=True)
            return
        extra_pairs, line_errors = parse_identity_lines(self.extras.value)
        pairs = [(self.character.value, self.island.value), *extra_pairs]
        if line_errors:
            embed = discord.Embed(
                title="Couldn't build the nickname",
                description="\n".join(f"• {e}" for e in line_errors),
                color=COLOR_BAD,
            )
            values = (self.character.value, self.island.value, self.extras.value or "")
            view = PreviewView(self.session, None, pairs, values)
        else:
            embed, view = build_preview(target, self.session, pairs, interaction.client)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class KeepIdentitiesSelect(discord.ui.Select):
    """Over the limit: choose which identities to keep (at most the allowed count)."""

    def __init__(self, identities: list[tuple[str, str]], keep_limit: int):
        self.identities = identities[:25]
        options = [
            discord.SelectOption(label=f"{c} | {i}"[:100], value=str(n))
            for n, (c, i) in enumerate(self.identities)
        ]
        super().__init__(
            placeholder=f"Keep up to {keep_limit} identit{'y' if keep_limit == 1 else 'ies'}…",
            min_values=1,
            max_values=max(1, min(keep_limit, len(options))),
            options=options,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        view: PreviewView = self.view
        if _not_yours(interaction, view.session):
            await interaction.response.send_message("This preview isn't yours.", ephemeral=True)
            return
        target = await _resolve_target(interaction, view.session)
        if target is None:
            await interaction.response.edit_message(content="That member isn't in the server anymore.", embed=None, view=None)
            return
        kept = [self.identities[int(v)] for v in self.values]
        embed, new_view = build_preview(target, view.session, kept, interaction.client)
        await interaction.response.edit_message(embed=embed, view=new_view)


class PreviewView(discord.ui.View):
    """Confirm / Edit / Cancel (+ keep-which picker when over the limit). Ephemeral, not persistent."""

    def __init__(self, session: NickSession, nickname: str | None, pairs: list[tuple[str, str]],
                 values: tuple[str, str, str], keep_limit: int = 0):
        super().__init__(timeout=300)
        self.session = session
        self.nickname = nickname
        self.values = values
        if nickname is None:
            self.remove_item(self.confirm)
        identities = flatten_identities(pairs)
        if keep_limit and len(identities) > keep_limit:
            self.add_item(KeepIdentitiesSelect(identities, keep_limit))

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success, emoji="✅", row=0)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if _not_yours(interaction, self.session) or not self.nickname:
            await interaction.response.send_message("This preview isn't yours.", ephemeral=True)
            return
        target = await _resolve_target(interaction, self.session)
        if target is None:
            await interaction.response.edit_message(content="That member isn't in the server anymore.", embed=None, view=None)
            return
        # Re-check: subscriptions may have changed since the preview
        report = identity_report(target, self.nickname, interaction.client)
        if report["over_limit"]:
            embed, view = build_preview(target, self.session, nickname_to_pairs(self.nickname), interaction.client)
            await interaction.response.edit_message(embed=embed, view=view)
            return
        old = target.nick
        reason = f"Nickname panel (by {interaction.user} {interaction.user.id})" if self.session.for_other else "Nickname panel"
        try:
            await target.edit(nick=self.nickname, reason=reason)
        except discord.Forbidden:
            await interaction.response.edit_message(
                embed=discord.Embed(
                    title="Couldn't change the nickname",
                    description=(
                        "I don't have permission. This happens for the server owner and members with a role "
                        f"above mine. Set it by hand to `{self.nickname}`."
                    ),
                    color=COLOR_BAD,
                ),
                view=None,
            )
            return
        except discord.HTTPException as exc:
            logger.error(f"[NICK] Failed to set nickname for {target.id}: {exc}")
            await interaction.response.edit_message(
                embed=discord.Embed(
                    title="Something went wrong",
                    description="Couldn't update the nickname. Please try again or contact staff.",
                    color=COLOR_BAD,
                ),
                view=None,
            )
            return

        self.stop()
        done = f"{target.mention} is now **{self.nickname}**." if self.session.for_other else f"You're now **{self.nickname}**."
        await interaction.response.edit_message(
            embed=discord.Embed(title="Nickname updated", description=done, color=COLOR_OK), view=None
        )
        logger.info(
            f"[NICK] {interaction.user} ({interaction.user.id}) set nickname of {target} ({target.id}) "
            f"via panel: {old!r} -> {self.nickname!r}"
        )

    @discord.ui.button(label="Edit", style=discord.ButtonStyle.secondary, emoji="✏️", row=0)
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        if _not_yours(interaction, self.session):
            await interaction.response.send_message("This preview isn't yours.", ephemeral=True)
            return
        await interaction.response.send_modal(NicknameModal(self.session, *self.values))

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=0)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(
            embed=discord.Embed(description="Cancelled. The nickname wasn't changed.", color=COLOR_PANEL), view=None
        )


class SavedCharactersSelect(discord.ui.Select):
    def __init__(self, saved: list[tuple[str, str]]):
        self.saved = saved[:25]
        options = [
            discord.SelectOption(label=f"{c} | {i}"[:100], value=str(n), emoji="🏝️")
            for n, (c, i) in enumerate(self.saved)
        ]
        super().__init__(
            placeholder="Pick saved characters…",
            min_values=1,
            max_values=len(options),
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        view: SavedCharactersView = self.view
        if _not_yours(interaction, view.session):
            await interaction.response.send_message("This isn't yours.", ephemeral=True)
            return
        target = await _resolve_target(interaction, view.session)
        if target is None:
            await interaction.response.edit_message(content="That member isn't in the server anymore.", embed=None, view=None)
            return
        chosen = [self.saved[int(v)] for v in self.values]
        embed, preview = build_preview(target, view.session, chosen, interaction.client)
        await interaction.response.edit_message(embed=embed, view=preview)


class SavedCharactersView(discord.ui.View):
    """Choose from website-saved characters, or type the nickname instead."""

    def __init__(self, session: NickSession, saved: list[tuple[str, str]], prefill: tuple[str, str, str]):
        super().__init__(timeout=300)
        self.session = session
        self.prefill = prefill
        self.add_item(SavedCharactersSelect(saved))

    @discord.ui.button(label="Type it instead", style=discord.ButtonStyle.secondary, emoji="✏️", row=1)
    async def type_instead(self, interaction: discord.Interaction, button: discord.ui.Button):
        if _not_yours(interaction, self.session):
            await interaction.response.send_message("This isn't yours.", ephemeral=True)
            return
        await interaction.response.send_modal(NicknameModal(self.session, *self.prefill))


def _prefill(member: discord.Member) -> tuple[str, str, str]:
    return pairs_to_form_values(nickname_to_pairs(member.nick or member.display_name))


async def start_nickname_flow(interaction: discord.Interaction, target: discord.Member) -> None:
    """Open the saved-character picker when the target has website characters, else the form."""
    session = NickSession(target_id=target.id, actor_id=interaction.user.id)
    prefill = _prefill(target)
    saved = await asyncio.to_thread(load_saved_characters, target.id)
    if not saved:
        await interaction.response.send_modal(NicknameModal(session, *prefill))
        return
    whose = f"{target.display_name}'s" if session.for_other else "your"
    embed = discord.Embed(
        title="Use saved characters?",
        description=(
            f"These are {whose} characters saved on the Chopaeng website. Pick the ones to put in the "
            "nickname, or type it instead."
        ),
        color=COLOR_PANEL,
    )
    await interaction.response.send_message(embed=embed, view=SavedCharactersView(session, saved, prefill), ephemeral=True)


class NicknamePanelView(discord.ui.View):
    """Persistent panel buttons (stable custom_ids, no timeout)."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Set Nickname", style=discord.ButtonStyle.primary, emoji="✏️", custom_id="nick_panel_set")
    async def set_nickname(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = _member_or_none(interaction)
        if member is None:
            await interaction.response.send_message("Use this in the server.", ephemeral=True)
            return
        await start_nickname_flow(interaction, member)

    @discord.ui.button(label="Check My Nickname", style=discord.ButtonStyle.secondary, emoji="🔍", custom_id="nick_panel_check")
    async def check_nickname(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = _member_or_none(interaction)
        if member is None:
            await interaction.response.send_message("Use this in the server.", ephemeral=True)
            return
        report = identity_report(member, member.display_name, interaction.client)
        if not report["valid"]:
            embed = discord.Embed(
                title="Your nickname isn't in the Camp format",
                description=(
                    f"**Current:** `{member.display_name}`\n\n"
                    "The flight logger can't match you to your flights. Press **Set Nickname** to fix it."
                ),
                color=COLOR_WARN,
            )
        else:
            over = report["over_limit"]
            embed = discord.Embed(
                title="⚠️ You'll be flagged on flights" if over else "✅ Your nickname looks good",
                description=f"**Current:** `{member.display_name}`\n\n" + "\n".join(_report_lines(report)),
                color=COLOR_BAD if over else COLOR_OK,
            )
            if over:
                embed.add_field(name="Why", value=_limit_message(report), inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Rules", style=discord.ButtonStyle.secondary, emoji="❓", custom_id="nick_panel_rules")
    async def rules(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(RULES_TEXT, ephemeral=True)


# ---------------------------------------------------------------------------
# Mod tool: right-click a member -> Apps -> Fix Nickname
# ---------------------------------------------------------------------------

def can_fix_nicknames(member: discord.Member) -> bool:
    perms = getattr(member, "guild_permissions", None)
    return is_staff(member) or bool(perms and perms.manage_nicknames)


@app_commands.context_menu(name="Fix Nickname")
@app_commands.guild_only()
@app_commands.default_permissions(manage_nicknames=True)
async def fix_nickname_context_menu(interaction: discord.Interaction, member: discord.Member):
    actor = _member_or_none(interaction)
    if actor is None or not can_fix_nicknames(actor):
        await interaction.response.send_message("Only staff can fix other members' nicknames.", ephemeral=True)
        return
    if member.bot:
        await interaction.response.send_message("That's a bot.", ephemeral=True)
        return
    await start_nickname_flow(interaction, member)


def register_nickname_commands(bot) -> None:
    """Persistent panel buttons + the Fix Nickname context menu. Call from setup_hook before syncing."""
    bot.add_view(NicknamePanelView())
    if bot.tree.get_command("Fix Nickname", type=discord.AppCommandType.user) is None:
        bot.tree.add_command(fix_nickname_context_menu)
