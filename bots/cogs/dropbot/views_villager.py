"""Villager injection UI: pickers, confirm views, Sanrio wizard and the guarded _start_injection."""

from __future__ import annotations

import asyncio
import discord
import logging
import re
from utils.acnh_catalog import ACNHCatalog

from .constants import PANEL_EMBED_COLOR, PANEL_EMBED_COLOR_WARN, PANEL_FOOTER_ICON, PANEL_FOOTER_TEXT, PLACEHOLDER_VILLAGERS, PLOT_MAX, PLOT_MIN, SANRIO_EMBED_COLOR, SANRIO_VILLAGERS, get_canonical_sanrio_name, is_sanrio_villager
from .db import _clear_sanrio_wizard_state, _load_sanrio_wizard_state, _save_sanrio_wizard_state
from .islands import _canonical_island, _island_url
from .parsing import _match_villagers
from .status import _err_embed, build_villager_picker_embed
from .ui_base import _OwnedView, _adopt, _reply
from .actions import _fetch_residents, _injection_guard, _perform_injection

logger = logging.getLogger("DropBotCog")


async def _start_injection(
    interaction: discord.Interaction,
    island_name: str,
    villager: str,
    plot: int | None = None,
) -> None:
    """Unified entry point for all villager injections.
    
    Checks is_sanrio_villager first. If Sanrio, launches the hot-swap wizard
    (ignoring the plot argument because hot-swap uses Plot 1).
    Otherwise executes the regular injection.
    """
    if is_sanrio_villager(villager):
        canonical = get_canonical_sanrio_name(villager)
        saved = await asyncio.to_thread(_load_sanrio_wizard_state, interaction.user.id, island_name)
        step1_done = False
        placeholder = "Raymond"
        if saved and saved.get("target_villager", "").lower() == canonical.lower():
            step1_done = bool(saved.get("step1_done"))
            placeholder = saved.get("placeholder_villager") or "Raymond"
        wizard = SanrioInjectionWizardView(
            island_name=island_name,
            user_id=interaction.user.id,
            target_villager=canonical,
            placeholder_villager=placeholder,
            step1_done=step1_done,
        )
        if step1_done:
            wizard.last_status = "✈️ **Resumed Session (Step 2 Ready):** Pick up right where you left off!"
        embed = wizard.build_embed()
        if interaction.response.is_done():
            await interaction.edit_original_response(content=None, embed=embed, view=_adopt(wizard, interaction))
        elif interaction.type == discord.InteractionType.component:
            await interaction.response.edit_message(content=None, embed=embed, view=_adopt(wizard, interaction))
        else:
            await interaction.response.send_message(embed=embed, view=_adopt(wizard, interaction), ephemeral=True)
        return

    # Guardrails need a live lookup, which can outlast Discord's 3s response window, so acknowledge first.
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=interaction.type != discord.InteractionType.component)

    canonical_island = _canonical_island(island_name) or island_name
    residents = await _fetch_residents(canonical_island)
    blocked, confirm = _injection_guard(canonical_island, villager, plot, residents)
    if blocked:
        await interaction.edit_original_response(content=None, embed=blocked, view=None)
        return
    if confirm:
        view = InjectionConfirmView(interaction.user.id, island_name, villager, plot, replacing=confirm[1])
        await interaction.edit_original_response(
            content=None, embed=confirm[0], view=_adopt(view, interaction)
        )
        return

    embed, _ = await _perform_injection(interaction.user, island_name, villager, plot)
    await interaction.edit_original_response(content=None, embed=embed, view=None)


class CustomAmiiboModal(discord.ui.Modal, title="✏️ Custom Amiibo / Villager"):
    name_input = discord.ui.TextInput(
        label="Target Amiibo / Villager Name",
        placeholder="e.g. Chai, Chelsea, Cece, Viché, W. Link",
        min_length=2,
        max_length=40,
        required=True,
    )

    def __init__(self, wizard_view: "SanrioInjectionWizardView"):
        super().__init__()
        self.wizard_view = wizard_view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw_name = self.name_input.value.strip()
        exact, _ = _match_villagers(raw_name)
        target = exact or raw_name.title()
        if is_sanrio_villager(target):
            target = get_canonical_sanrio_name(target)
        self.wizard_view.target_villager = target
        await asyncio.to_thread(
            _save_sanrio_wizard_state,
            self.wizard_view.user_id,
            self.wizard_view.island_name,
            self.wizard_view.target_villager,
            self.wizard_view.placeholder_villager,
            self.wizard_view.step1_done,
        )
        self.wizard_view._rebuild_items()
        await interaction.response.edit_message(
            embed=self.wizard_view.build_embed(),
            view=self.wizard_view,
        )


class CustomPlaceholderModal(discord.ui.Modal, title="✏️ Custom Plot 1 Placeholder"):
    name_input = discord.ui.TextInput(
        label="Placeholder Villager Name (Plot 1)",
        placeholder="e.g. Raymond, Marshal, Shino, Sasha",
        min_length=2,
        max_length=40,
        required=True,
    )

    def __init__(self, wizard_view: "SanrioInjectionWizardView"):
        super().__init__()
        self.wizard_view = wizard_view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw_name = self.name_input.value.strip()
        exact, _ = _match_villagers(raw_name)
        self.wizard_view.placeholder_villager = exact or raw_name.title()
        await asyncio.to_thread(
            _save_sanrio_wizard_state,
            self.wizard_view.user_id,
            self.wizard_view.island_name,
            self.wizard_view.target_villager,
            self.wizard_view.placeholder_villager,
            self.wizard_view.step1_done,
        )
        self.wizard_view._rebuild_items()
        await interaction.response.edit_message(
            embed=self.wizard_view.build_embed(),
            view=self.wizard_view,
        )


class InjectionConfirmView(_OwnedView):
    """Confirm an injection that would replace a known resident."""

    def __init__(self, user_id: int, island_name: str, villager: str, plot: int | None, replacing: str):
        super().__init__(user_id=user_id, timeout=90)
        self.island_name = island_name
        self.villager = villager
        self.plot = plot
        self.replacing = replacing

        confirm = discord.ui.Button(
            label=f"Replace {replacing}"[:80], emoji="⚡", style=discord.ButtonStyle.danger, row=0
        )
        confirm.callback = self._on_confirm  # type: ignore[assignment]
        self.add_item(confirm)
        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary, row=0)
        cancel.callback = self._on_cancel  # type: ignore[assignment]
        self.add_item(cancel)

    async def _on_confirm(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.defer()
        embed, ok = await _perform_injection(interaction.user, self.island_name, self.villager, self.plot)
        if ok:
            embed.add_field(name="Replaced", value=f"{self.replacing} → {self.villager}", inline=False)
        await interaction.edit_original_response(content=None, embed=embed, view=None)

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(content="Injection cancelled.", embed=None, view=None)


class SanrioInjectionWizardView(_OwnedView):
    """
    Interactive 2-step live hot-swap wizard for Sanrio and Amiibo villagers.
    Progress is persisted to the DB so users can resume after flights.
    """

    def __init__(
        self,
        island_name: str,
        user_id: int,
        target_villager: str = "Chai",
        placeholder_villager: str = "Raymond",
        step1_done: bool = False,
    ):
        super().__init__(user_id=user_id, timeout=900)  # 15 min — matches interaction token lifetime
        self.island_name = island_name
        self.target_villager = get_canonical_sanrio_name(target_villager) if is_sanrio_villager(target_villager) else target_villager.title()
        self.placeholder_villager = placeholder_villager.title()
        self.step1_done = step1_done
        self.step2_done = False
        self.presence_warned = False
        self.last_status: str | None = None
        self._rebuild_items()

    def _rebuild_items(self) -> None:
        self.clear_items()

        # Row 0: Target Sanrio / Amiibo Select
        target_opts: list[discord.SelectOption] = []
        in_presets = False
        for name, desc, emoji in SANRIO_VILLAGERS:
            is_def = (name.lower() == self.target_villager.lower())
            if is_def:
                in_presets = True
            target_opts.append(
                discord.SelectOption(
                    label=name,
                    description=desc[:100],
                    emoji=emoji,
                    value=name,
                    default=is_def,
                )
            )
        if not in_presets:
            target_opts.insert(
                0,
                discord.SelectOption(
                    label=self.target_villager[:100],
                    description="Custom target character"[:100],
                    emoji="✨",
                    value=self.target_villager[:100],
                    default=True,
                ),
            )
        target_opts.append(
            discord.SelectOption(
                label="Type Custom Amiibo Name…",
                description="Enter any custom amiibo character"[:100],
                emoji="✏️",
                value="custom",
            )
        )
        target_select = discord.ui.Select(
            placeholder=f"🎯 Target: {self.target_villager} (Plot 1)",
            options=target_opts[:25],
            row=0,
            disabled=self.step2_done,
        )
        target_select.callback = self._on_target_select
        self.add_item(target_select)

        # Row 1: Placeholder Select (Plot 1)
        ph_opts: list[discord.SelectOption] = []
        ph_in_presets = False
        for name, desc, emoji in PLACEHOLDER_VILLAGERS:
            is_def = (name.lower() == self.placeholder_villager.lower())
            if is_def:
                ph_in_presets = True
            ph_opts.append(
                discord.SelectOption(
                    label=name,
                    description=desc[:100],
                    emoji=emoji,
                    value=name,
                    default=is_def,
                )
            )
        if not ph_in_presets:
            ph_opts.insert(
                0,
                discord.SelectOption(
                    label=self.placeholder_villager[:100],
                    description="Custom placeholder for Plot 1"[:100],
                    emoji="🐱",
                    value=self.placeholder_villager[:100],
                    default=True,
                ),
            )
        ph_opts.append(
            discord.SelectOption(
                label="Type Custom Placeholder…",
                description="Enter any villager to hold Plot 1"[:100],
                emoji="✏️",
                value="custom",
            )
        )
        placeholder_select = discord.ui.Select(
            placeholder=f"📦 Placeholder: {self.placeholder_villager} (Plot 1)",
            options=ph_opts[:25],
            row=1,
            disabled=self.step1_done or self.step2_done,
        )
        placeholder_select.callback = self._on_placeholder_select
        self.add_item(placeholder_select)

        # Row 2: Step 1 and Step 2 Action Buttons
        step1_label = f"1️⃣ Inject {self.placeholder_villager} (Plot 1)"[:80]
        step1_btn = discord.ui.Button(
            label=step1_label,
            style=discord.ButtonStyle.primary if not self.step1_done else discord.ButtonStyle.secondary,
            disabled=self.step1_done or self.step2_done,
            row=2,
        )
        step1_btn.callback = self._on_step1
        self.add_item(step1_btn)

        step2_label = f"2️⃣ Inject {self.target_villager} (Live on Island)"[:80]
        step2_btn = discord.ui.Button(
            label=step2_label,
            style=discord.ButtonStyle.success if self.step1_done else discord.ButtonStyle.secondary,
            disabled=(not self.step1_done) or self.step2_done,
            row=2,
        )
        step2_btn.callback = self._on_step2
        self.add_item(step2_btn)

        if not self.step1_done and not self.step2_done:
            skip_btn = discord.ui.Button(
                label="I already did Step 1",
                emoji="⏭️",
                style=discord.ButtonStyle.secondary,
                row=2,
            )
            skip_btn.callback = self._on_skip_step1
            self.add_item(skip_btn)
        elif self.step1_done and not self.step2_done and getattr(self, "presence_warned", False):
            force_btn = discord.ui.Button(
                label="⚠️ Confirm On Island, Inject",
                style=discord.ButtonStyle.danger,
                row=2,
            )
            force_btn.callback = self._on_force_step2
            self.add_item(force_btn)

        # Row 3: Navigation Buttons
        back_btn = discord.ui.Button(
            label="Regular Villagers",
            emoji="🔙",
            style=discord.ButtonStyle.secondary,
            row=3,
        )
        back_btn.callback = self._on_back
        self.add_item(back_btn)

        close_btn = discord.ui.Button(
            label="Close",
            emoji="✖",
            style=discord.ButtonStyle.secondary,
            row=3,
        )
        close_btn.callback = self._on_close
        self.add_item(close_btn)

    def build_embed(self) -> discord.Embed:
        if not self.step1_done and not self.step2_done:
            s1, s2, s3, s4 = "👉", "⬜", "⬜", "⬜"
        elif self.step1_done and not self.step2_done:
            s1, s2, s3, s4 = "✅", "👉", "⬜", "⬜"
        else:
            s1, s2, s3, s4 = "✅", "✅", "✅", "👉"

        tracker = (
            f"{s1} **1.** Inject {self.placeholder_villager} → Plot 1\n"
            f"{s2} **2.** Fly to {self.island_name}\n"
            f"{s3} **3.** Inject {self.target_villager} (live)\n"
            f"{s4} **4.** Talk to Plot 1 resident"
        )

        embed = discord.Embed(
            title=f"🎀 Sanrio & Amiibo Injection Wizard — {self.island_name}",
            description=(
                f"{tracker}\n\n"
                f"⚠️ **Important:** Don't leave Plot 1 empty or move the placeholder out before Step 2."
            ),
            color=SANRIO_EMBED_COLOR,
        )

        if not self.step1_done and not self.step2_done:
            curr = f"Click **1️⃣ Inject {self.placeholder_villager}** before boarding your flight."
        elif self.step1_done and not self.step2_done:
            curr = f"Fly to **[{self.island_name}]({_island_url(self.island_name)})** now! Once you land, click **2️⃣ Inject {self.target_villager}**."
        else:
            curr = "🎉 **VILLAGER INJECTED!** Walk into Plot 1's house on the island, talk to the resident inside, and invite them to move in!"

        embed.add_field(name="📌 Current Step", value=curr, inline=False)

        if self.last_status:
            embed.add_field(name="📢 Latest Update", value=self.last_status[:1024], inline=False)

        embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
        return embed

    async def _on_skip_step1(self, interaction: discord.Interaction) -> None:
        self.step1_done = True
        self.last_status = "⏭️ Marked Step 1 as done. Click **2️⃣ Inject** once you land on the island!"
        await asyncio.to_thread(
            _save_sanrio_wizard_state,
            self.user_id,
            self.island_name,
            self.target_villager,
            self.placeholder_villager,
            True,
        )
        self._rebuild_items()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _on_target_select(self, interaction: discord.Interaction) -> None:
        vals = (interaction.data or {}).get("values", [])
        val = vals[0] if vals else ""
        if not val:
            return
        if val == "custom":
            await interaction.response.send_modal(CustomAmiiboModal(self))
            return
        self.target_villager = val
        await asyncio.to_thread(
            _save_sanrio_wizard_state,
            self.user_id,
            self.island_name,
            self.target_villager,
            self.placeholder_villager,
            self.step1_done,
        )
        self._rebuild_items()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _on_placeholder_select(self, interaction: discord.Interaction) -> None:
        vals = (interaction.data or {}).get("values", [])
        val = vals[0] if vals else ""
        if not val:
            return
        if val == "custom":
            await interaction.response.send_modal(CustomPlaceholderModal(self))
            return
        self.placeholder_villager = val
        await asyncio.to_thread(
            _save_sanrio_wizard_state,
            self.user_id,
            self.island_name,
            self.target_villager,
            self.placeholder_villager,
            self.step1_done,
        )
        self._rebuild_items()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _on_step1(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        embed_res, ok = await _perform_injection(
            interaction.user,
            self.island_name,
            self.placeholder_villager,
            plot=1,
            cooldown_key="sanrio1",
        )
        if ok:
            self.step1_done = True
            await asyncio.to_thread(
                _save_sanrio_wizard_state,
                self.user_id,
                self.island_name,
                self.target_villager,
                self.placeholder_villager,
                True,
            )
            self.last_status = (
                f"✅ **Placeholder Injected!** `{self.placeholder_villager}` is queued in Plot 1.\n"
                f"🛫 **Take flight to [{self.island_name}]({_island_url(self.island_name)}) now!**\n"
                f"Once you land on the island, click **Step 2** below."
            )
        else:
            reason = embed_res.description or "Bot did not respond"
            self.last_status = f"⚠️ **Step 1 Error:** {reason}"

        self._rebuild_items()
        await interaction.edit_original_response(embed=self.build_embed(), view=self)

    async def _on_step2(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        from utils.island_presence import verify_user_on_island_async

        is_admin = False
        guild_perms = getattr(interaction.user, "guild_permissions", None)
        if guild_perms and getattr(guild_perms, "administrator", None) is True:
            is_admin = True

        is_present, _, _ = await verify_user_on_island_async(
            self.island_name, user=interaction.user, allow_admin_bypass=True
        )
        if not is_present and not is_admin and not getattr(self, "presence_warned", False):
            self.presence_warned = True
            self.last_status = (
                f"✈️ **Not detected on {self.island_name} yet!**\n"
                f"Please fly into **{self.island_name}** and step outside the airport gates before running Step 2.\n"
                f"*If you are already outside the airport gates, click **⚠️ Confirm On Island, Inject**.*"
            )
            self._rebuild_items()
            await interaction.edit_original_response(embed=self.build_embed(), view=self)
            return

        embed_res, ok = await _perform_injection(
            interaction.user,
            self.island_name,
            self.target_villager,
            plot=1,
            cooldown_key="sanrio2",
        )
        if ok:
            self.step2_done = True
            await asyncio.to_thread(_clear_sanrio_wizard_state, self.user_id, self.island_name)
            self.last_status = (
                f"🎉 **VILLAGER INJECTED!**\n\n"
                f"🏠 **Go to Plot 1's house now!**\n"
                f"• Enter Plot 1's house on the island.\n"
                f"• Talk to the resident inside *(they will visually look like `{self.placeholder_villager}`)*.\n"
                f"• Invite them to move to your island! 🎀"
            )
        else:
            reason = embed_res.description or "Bot did not respond"
            self.last_status = f"⚠️ **Step 2 Error:** {reason}"

        self._rebuild_items()
        await interaction.edit_original_response(embed=self.build_embed(), view=self)

    async def _on_force_step2(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        embed_res, ok = await _perform_injection(
            interaction.user,
            self.island_name,
            self.target_villager,
            plot=1,
            cooldown_key="sanrio2",
        )
        if ok:
            self.step2_done = True
            await asyncio.to_thread(_clear_sanrio_wizard_state, self.user_id, self.island_name)
            self.last_status = (
                f"🎉 **VILLAGER INJECTED!**\n\n"
                f"🏠 **Go to Plot 1's house now!**\n"
                f"• Enter Plot 1's house on the island.\n"
                f"• Talk to the resident inside *(they will visually look like `{self.placeholder_villager}`)*.\n"
                f"• Invite them to move to your island! 🎀"
            )
        else:
            reason = embed_res.description or "Bot did not respond"
            self.last_status = f"⚠️ **Step 2 Error:** {reason}"

        self._rebuild_items()
        await interaction.edit_original_response(embed=self.build_embed(), view=self)

    async def _on_back(self, interaction: discord.Interaction) -> None:
        picker = InteractiveVillagerPickerView(self.island_name, self.user_id)
        embed = build_villager_picker_embed(self.island_name)
        await interaction.response.edit_message(embed=embed, view=_adopt(picker, interaction))

    async def _on_close(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(content="Sanrio Wizard closed.", embed=None, view=None)
        self.stop()


class InjectVillagerModal(discord.ui.Modal, title="🐱 Inject Villager"):
    villager_input = discord.ui.TextInput(
        label="Villager name",
        style=discord.TextStyle.short,
        placeholder="e.g. Raymond, Marshal, Shino",
        min_length=2,
        max_length=64,
        required=True,
    )
    plot_input = discord.ui.TextInput(
        label=f"Plot ({PLOT_MIN}–{PLOT_MAX}, optional)",
        style=discord.TextStyle.short,
        placeholder="Leave blank for the next available plot",
        min_length=0,
        max_length=2,
        required=False,
    )

    def __init__(self, island_name: str):
        super().__init__()
        self.island_name = island_name

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        raw_name = self.villager_input.value.strip()
        raw_plot = self.plot_input.value.strip()

        plot: int | None = None
        if raw_plot:
            if not re.fullmatch(r"[0-9]{1,2}", raw_plot) or not PLOT_MIN <= int(raw_plot) <= PLOT_MAX:
                await interaction.followup.send(
                    f"❌ Plot must be a number from {PLOT_MIN} to {PLOT_MAX}.", ephemeral=True
                )
                return
            plot = int(raw_plot)

        exact, candidates = await asyncio.to_thread(_match_villagers, raw_name)

        if exact:
            await _start_injection(interaction, self.island_name, exact, plot)
            return

        if not candidates:
            await interaction.followup.send(
                f"❌ I couldn't find a villager called **{raw_name[:64]}**. Check the spelling and try again.",
                ephemeral=True,
            )
            return

        view = VillagerChoiceView(interaction.user.id, self.island_name, candidates, plot)
        prompt = discord.Embed(
            title="Did you mean…?",
            description=(
                f"I couldn't find **{raw_name[:64]}** exactly.\n"
                + (
                    f"Did you mean **{candidates[0]}**?"
                    if len(candidates) == 1
                    else "Pick the villager you meant from the list."
                )
            ),
            color=PANEL_EMBED_COLOR_WARN,
        )
        prompt.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
        await interaction.followup.send(embed=prompt, view=_adopt(view, interaction), ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logger.error("[DropBot] InjectVillagerModal error: %s", error, exc_info=error)
        await _reply(interaction, "❌ Something went wrong. Please try again.")


# ─── Ephemeral views ───────────────────────────────────────────────────────────

class VillagerChoiceView(_OwnedView):
    """Confirm (one candidate) or pick (several candidates) before injecting a villager."""

    def __init__(self, user_id: int, island_name: str, candidates: list[str], plot: int | None):
        super().__init__(user_id=user_id, timeout=90)
        self.island_name = island_name
        self.candidates = candidates
        self.plot = plot

        if len(candidates) == 1:
            confirm = discord.ui.Button(
                label=f"Yes, inject {candidates[0]}"[:80], style=discord.ButtonStyle.success, row=0
            )
            confirm.callback = self._on_confirm  # type: ignore[assignment]
            self.add_item(confirm)
        else:
            select = discord.ui.Select(
                placeholder="Choose a villager…",
                options=[discord.SelectOption(label=c[:100], value=c[:100]) for c in candidates[:25]],
                row=0,
            )
            select.callback = self._make_select_callback(select)  # type: ignore[assignment]
            self.add_item(select)

        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary, row=1)
        cancel.callback = self._on_cancel  # type: ignore[assignment]
        self.add_item(cancel)

    async def _run(self, interaction: discord.Interaction, villager: str) -> None:
        self.stop()
        await _start_injection(interaction, self.island_name, villager, self.plot)

    async def _on_confirm(self, interaction: discord.Interaction) -> None:
        await self._run(interaction, self.candidates[0])

    def _make_select_callback(self, select: discord.ui.Select):
        async def callback(interaction: discord.Interaction) -> None:
            await self._run(interaction, select.values[0])

        return callback

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(content="Cancelled.", embed=None, view=None)
        self.stop()


class VillagerPlotPickView(_OwnedView):
    """Pick plot (1–10 or next available) and confirm villager injection."""

    def __init__(self, user_id: int, island_name: str, villager_name: str):
        super().__init__(user_id=user_id, timeout=90)
        self.island_name = island_name
        self.villager_name = villager_name
        self.selected_plot: int | None = None

        plot_options = [
            discord.SelectOption(
                label="Next Available Plot (Default)",
                description="Automatically uses the next available house plot",
                value="none",
                default=True,
            )
        ] + [
            discord.SelectOption(
                label=f"Plot #{p}",
                description=f"House plot slot #{p}",
                value=str(p),
            )
            for p in range(PLOT_MIN, PLOT_MAX + 1)
        ]

        plot_select = discord.ui.Select(
            placeholder="Select house plot (optional)…",
            options=plot_options[:25],
            row=0,
        )
        plot_select.callback = self._on_plot_select
        self.add_item(plot_select)

        inject_btn = discord.ui.Button(
            label=f"Inject {villager_name}"[:80],
            emoji="⚡",
            style=discord.ButtonStyle.success,
            row=1,
        )
        inject_btn.callback = self._on_inject
        self.add_item(inject_btn)

        cancel_btn = discord.ui.Button(
            label="Cancel",
            emoji="✖",
            style=discord.ButtonStyle.secondary,
            row=1,
        )
        cancel_btn.callback = self._on_cancel
        self.add_item(cancel_btn)

    async def _on_plot_select(self, interaction: discord.Interaction) -> None:
        vals = (interaction.data or {}).get("values", ["none"])
        val = vals[0] if vals else "none"
        if val != "none" and val.isdigit():
            self.selected_plot = int(val)
        else:
            self.selected_plot = None
        if not interaction.response.is_done():
            await interaction.response.defer()

    async def _on_inject(self, interaction: discord.Interaction) -> None:
        self.stop()
        await _start_injection(
            interaction, self.island_name, self.villager_name, self.selected_plot
        )

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(content="Injection cancelled.", embed=None, view=None)
        self.stop()


class VillagerSearchModal(discord.ui.Modal, title="🔍 Search Villager"):
    search_query = discord.ui.TextInput(
        label="Villager Name or Keyword",
        placeholder="e.g. Raymond, Marshal, Cat, Rabbit, Peppy",
        required=True,
        max_length=60,
    )

    def __init__(self, island_name: str):
        super().__init__()
        self.island_name = island_name

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        raw_name = self.search_query.value.strip()

        exact, candidates = await asyncio.to_thread(_match_villagers, raw_name, 15)
        if exact:
            await _start_injection(interaction, self.island_name, exact)
            return

        if not candidates:
            await interaction.followup.send(
                embed=_err_embed(f"I couldn't find any villager matching **{raw_name[:50]}**. Check the spelling and try again."),
                ephemeral=True,
            )
            return

        view = VillagerChoiceView(interaction.user.id, self.island_name, candidates, None)
        prompt = discord.Embed(
            title="🔍 Villager Search Results",
            description=(
                f"Matching villagers for **{raw_name[:50]}**:\n"
                "Pick the villager you want to inject from the list below."
            ),
            color=PANEL_EMBED_COLOR,
        )
        prompt.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
        await interaction.followup.send(embed=prompt, view=_adopt(view, interaction), ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logger.error("[DropBot] VillagerSearchModal error: %s", error, exc_info=error)
        await _reply(interaction, "❌ Something went wrong searching villagers.")


class InteractiveVillagerPickerView(_OwnedView):
    """Interactive villager selection: top villagers dropdown, live search, manual text."""

    def __init__(self, island_name: str, user_id: int):
        super().__init__(user_id=user_id, timeout=180)
        self.island_name = island_name

        catalog = ACNHCatalog.get_instance()
        popular_villagers = catalog.get_popular_villagers()
        if not popular_villagers:
            from utils.acnh_catalog import POPULAR_VILLAGERS
            v_options = [
                discord.SelectOption(
                    label=name[:100],
                    description=desc[:100],
                    emoji=emoji_char,
                    value=name[:100],
                )
                for name, desc, emoji_char in POPULAR_VILLAGERS
            ]
        else:
            v_options = [
                discord.SelectOption(
                    label=v.name[:100],
                    description=(f"{v.personality} • {v.species}" if v.personality and v.species else v.species or "Villager")[:100],
                    emoji=v.emoji,
                    value=v.name[:100],
                )
                for v in popular_villagers
            ]
        v_options = v_options[:25]
        if v_options:
            v_select = discord.ui.Select(
                placeholder="🐱 Choose a popular villager to inject…",
                options=v_options,
                row=0,
            )
            v_select.callback = self._on_villager_select
            self.add_item(v_select)

        search_btn = discord.ui.Button(label="Search Villager", emoji="🔍", style=discord.ButtonStyle.primary, row=1)
        search_btn.callback = self._on_search_villager
        self.add_item(search_btn)

        sanrio_btn = discord.ui.Button(label="Sanrio / Amiibo", emoji="🎀", style=discord.ButtonStyle.success, row=1)
        sanrio_btn.callback = self._on_sanrio_wizard
        self.add_item(sanrio_btn)

        type_btn = discord.ui.Button(label="Type Any Name", emoji="✏️", style=discord.ButtonStyle.secondary, row=1)
        type_btn.callback = self._on_type_modal
        self.add_item(type_btn)

        close_btn = discord.ui.Button(label="Close", emoji="✖", style=discord.ButtonStyle.secondary, row=1)
        close_btn.callback = self._on_close
        self.add_item(close_btn)

    async def _on_villager_select(self, interaction: discord.Interaction) -> None:
        vals = (interaction.data or {}).get("values", [])
        selected_name = vals[0] if vals else ""
        if not selected_name:
            return
        if is_sanrio_villager(selected_name):
            await _start_injection(interaction, self.island_name, selected_name)
            return

        view = VillagerPlotPickView(
            user_id=self.user_id,
            island_name=self.island_name,
            villager_name=selected_name,
        )
        embed = discord.Embed(
            title=f"🐱 Inject {selected_name}",
            description=(
                f"Choose where to inject **{selected_name}** into **[{self.island_name}]({_island_url(self.island_name)})**.\n\n"
                f"• Click **⚡ Inject {selected_name}** for quick injection\n"
                f"• Or choose a specific plot ({PLOT_MIN}–{PLOT_MAX}) from the dropdown."
            ),
            color=PANEL_EMBED_COLOR,
        )
        embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
        await interaction.response.edit_message(embed=embed, view=_adopt(view, interaction))

    async def _on_sanrio_wizard(self, interaction: discord.Interaction) -> None:
        saved = await asyncio.to_thread(_load_sanrio_wizard_state, self.user_id, self.island_name)
        resumed = bool(saved and saved.get("step1_done"))
        wizard = SanrioInjectionWizardView(
            island_name=(saved or {}).get("island_name") or self.island_name,
            user_id=self.user_id,
            target_villager=(saved or {}).get("target_villager") or "Chai",
            placeholder_villager=(saved or {}).get("placeholder_villager") or "Raymond",
            step1_done=resumed,
        )
        if resumed:
            wizard.last_status = "✈️ **Resumed (Step 2 ready):** pick up where you left off."
        await interaction.response.edit_message(embed=wizard.build_embed(), view=wizard)

    async def _on_search_villager(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(VillagerSearchModal(island_name=self.island_name))

    async def _on_type_modal(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(InjectVillagerModal(island_name=self.island_name))

    async def _on_close(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(content="Menu closed.", embed=None, view=None)
        self.stop()
