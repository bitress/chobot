"""Catalog search, item detail, cart manager and the drop pickers."""

from __future__ import annotations

import asyncio
import contextlib
import discord
import logging
import math
import re
from collections import Counter
from typing import Any, TYPE_CHECKING
from utils.acnh_catalog import ACNHCatalog, CatalogItem, CatalogVillager
from utils.database import connect_db

from .constants import MAX_DROP_SLOTS, MAX_STACK, PANEL_EMBED_COLOR, PANEL_EMBED_COLOR_WARN, PANEL_FOOTER_ICON, PANEL_FOOTER_TEXT, is_sanrio_villager
from .db import _get_user_last_drop, _load_cart_rows
from .islands import _island_url
from .parsing import _normalize_items, _search_catalog, parse_drop_items
from .status import _err_embed, _presence_line, build_drop_picker_embed
from .ui_base import _OwnedView, _adopt, _reply
from .actions import _perform_drop
from .cart import _CartEntry, _HEX_INPUT_RE, _NO_PATTERN_LABEL, _PersistentCart, _cart_key, _cart_summary, _default_qty, _item_variants, _match_variant_index, _restore_cart_entries, _unique
from .views_villager import VillagerChoiceView, _start_injection

if TYPE_CHECKING:
    from .cart import _Variant

logger = logging.getLogger("DropBotCog")


# ─── Modals ───────────────────────────────────────────────────────────────────




class CustomDropModal(discord.ui.Modal, title="📦 Custom Item Drop"):
    def __init__(self, island_name: str, default_text: str = ""):
        super().__init__()
        self.island_name = island_name
        self.items_input = discord.ui.TextInput(
            label=f"Items to drop (up to {MAX_DROP_SLOTS} items)",
            style=discord.TextStyle.paragraph,
            placeholder="Gold nugget 30, Iron nugget 30, 09C9 30",
            default=default_text or None,
            min_length=2,
            max_length=500,
            required=True,
        )
        self.add_item(self.items_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        parsed, err = parse_drop_items(self.items_input.value)
        if err or not parsed:
            retry_view = RetryDropView(
                island=self.island_name, text=self.items_input.value, user_id=interaction.user.id
            )
            await interaction.followup.send(
                f"❌ {err}\n\nPress **Fix & retry** to re-open the form with your input pre-filled.",
                view=_adopt(retry_view, interaction),
                ephemeral=True,
            )
            return

        # Human-readable summary for success; hex payload for SysBot
        display_contents = ", ".join(f"{name} ×{qty}" for name, qty in parsed)
        normalized = _normalize_items(parsed)
        embed, _ = await _perform_drop(
            interaction.user,
            self.island_name,
            normalized,
            heading="📦 Drop queued",
            contents=display_contents,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logger.error("[DropBot] CustomDropModal error: %s", error, exc_info=error)
        await _reply(interaction, "❌ Something went wrong. Please try again.")


class RetryDropView(_OwnedView):
    """Shown when CustomDropModal validation fails — pre-fills the modal so users don't lose their work."""

    def __init__(self, island: str, text: str, user_id: int):
        super().__init__(user_id=user_id, timeout=120)
        self.island = island
        self.text = text

    @discord.ui.button(label="Fix & retry", emoji="✏️", style=discord.ButtonStyle.primary)
    async def retry(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(CustomDropModal(island_name=self.island, default_text=self.text))


class ItemHexModal(discord.ui.Modal, title="#️⃣ Custom Item Hex"):
    """Override the hex that an item drops as. Blank resets to the catalog hex."""

    def __init__(self, item_view: "ItemDetailView"):
        super().__init__()
        self.item_view = item_view
        current = item_view.drop_hex
        self.hex_input = discord.ui.TextInput(
            label="Hex ID to drop (4 or 16 characters)",
            placeholder="e.g. 0B64 or 0000002000003A5E (blank = reset)",
            default=current or None,
            max_length=24,
            required=False,
        )
        self.add_item(self.hex_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = re.sub(r"[\s_\-]", "", self.hex_input.value or "").upper()
        if raw.startswith("0X"):
            raw = raw[2:]
        catalog_hex = (self.item_view.current_item.to_hex() or "").upper()
        if not raw or raw == catalog_hex:
            self.item_view.hex_override = None
            notice = f"↩️ Reset to catalog hex `{catalog_hex or self.item_view.drop_hex}`."
        elif not _HEX_INPUT_RE.match(raw):
            await _reply(interaction, embed=_err_embed(
                f"`{raw[:24]}` isn't a valid item hex.",
                hint="Use exactly **4** or **16** hex characters (0–9, A–F), e.g. `0B64` or `0000002000003A5E`.",
            ))
            return
        else:
            self.item_view.hex_override = raw
            notice = f"#️⃣ Custom drop hex set to `{raw}`."
        await self.item_view.refresh(interaction, notice)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logger.error("[DropBot] ItemHexModal error: %s", error, exc_info=error)
        await _reply(interaction, "❌ Something went wrong setting the hex code.")


class ItemDetailView(_OwnedView):
    """
    Focused Item Detail Card (ChoPaeng Command Builder style):
    - Live thumbnail & hex preview
    - Color dropdown & pattern dropdown
    - Quantity stepper: [ − ] [ ×qty ] [ + ] [ ×1 ] [ ×Max ]
    - Actions:
      ⚡ Drop Now (1-click drop without needing a cart)
      🛒 Add to Cart (or Update Cart)
      🔙 Back to Results
      #️⃣ Custom Hex
      🗑️ Remove (if editing an existing cart item)
    """

    def __init__(
        self,
        search_view: "CatalogSearchView",
        item: CatalogItem,
        *,
        cart_key: str | None = None,
        initial_qty: int | None = None,
        initial_hex_override: str | None = None,
    ):
        super().__init__(user_id=search_view.user_id, timeout=300)
        self.search_view = search_view
        self.base_item = item
        self.cart_key = cart_key
        self.variants: list[_Variant] = _item_variants(item)

        if self.variants:
            idx = _match_variant_index(item, self.variants)
            self.current_variant: _Variant | None = self.variants[idx]
            self.current_item: CatalogItem = self.variants[idx].item
        else:
            self.current_variant = None
            self.current_item = item

        if initial_qty is not None:
            self.qty = max(1, min(MAX_STACK, initial_qty))
        elif cart_key and cart_key in search_view.cart:
            self.qty = search_view.cart[cart_key].qty
        else:
            self.qty = _default_qty(self.current_item)

        if initial_hex_override is not None:
            self.hex_override: str | None = initial_hex_override
        elif cart_key and cart_key in search_view.cart:
            self.hex_override = search_view.cart[cart_key].hex_override
        else:
            self.hex_override = None

        self._rebuild()

    @property
    def drop_hex(self) -> str:
        return self.hex_override or self.current_item.to_hex() or self.current_item.internal_id or self.current_item.name

    @property
    def current_key(self) -> str:
        return _cart_key(self.current_item)

    @property
    def is_in_cart(self) -> bool:
        return (bool(self.cart_key and self.cart_key in self.search_view.cart) or
                (self.current_key in self.search_view.cart))

    @property
    def bodies(self) -> list[str]:
        return _unique([v.body for v in self.variants])

    @property
    def patterns(self) -> list[str]:
        return _unique([v.pattern for v in self.variants])

    def _find(self, body: str, pattern: str) -> _Variant | None:
        exact = next((v for v in self.variants if v.body == body and v.pattern == pattern), None)
        if exact:
            return exact
        return next((v for v in self.variants if v.body == body), None) if body else \
            next((v for v in self.variants if v.pattern == pattern), None)

    def _rebuild(self) -> None:
        self.clear_items()
        bodies, patterns = self.bodies, self.patterns
        cur = self.current_variant

        # Row 0: Color Select (if > 1 color)
        if len(bodies) > 1:
            opts = []
            for idx, body in enumerate(bodies[:25]):
                sample = next(v for v in self.variants if v.body == body)
                opts.append(discord.SelectOption(
                    label=body[:100],
                    value=str(idx),
                    description=(f"Hex: {sample.item.to_hex()}" if not sample.pattern or len(patterns) <= 1
                                 else f"{sum(1 for v in self.variants if v.body == body)} pattern(s)")[:100],
                    default=bool(cur and cur.body == body),
                ))
            sel = discord.ui.Select(
                placeholder=f"🎨 Color: {cur.body if cur else 'Pick color'}"[:100],
                options=opts,
                row=0,
            )
            sel.callback = self.on_body_select  # type: ignore[assignment]
            self.add_item(sel)

        # Row 1: Pattern Select (if > 1 pattern)
        if len(patterns) > 1:
            opts = []
            for idx, pat in enumerate(patterns[:25]):
                opts.append(discord.SelectOption(
                    label=(pat or _NO_PATTERN_LABEL)[:100],
                    value=str(idx),
                    default=bool(cur and cur.pattern == pat),
                ))
            sel = discord.ui.Select(
                placeholder=f"🖌️ Pattern: {(cur.pattern or _NO_PATTERN_LABEL) if cur else 'Pick pattern'}"[:100],
                options=opts,
                row=1,
            )
            sel.callback = self.on_pattern_select  # type: ignore[assignment]
            self.add_item(sel)

        # Row 2: Quantity buttons
        minus = discord.ui.Button(label="−", style=discord.ButtonStyle.secondary, disabled=self.qty <= 1, row=2)
        minus.callback = self.on_minus  # type: ignore[assignment]
        self.add_item(minus)

        self.add_item(discord.ui.Button(label=f"×{self.qty}", style=discord.ButtonStyle.secondary, disabled=True, row=2))

        plus = discord.ui.Button(label="+", style=discord.ButtonStyle.secondary, disabled=self.qty >= MAX_STACK, row=2)
        plus.callback = self.on_plus  # type: ignore[assignment]
        self.add_item(plus)

        one = discord.ui.Button(label="×1", style=discord.ButtonStyle.secondary, disabled=self.qty == 1, row=2)
        one.callback = self.on_qty_one  # type: ignore[assignment]
        self.add_item(one)

        mx = discord.ui.Button(label=f"×{MAX_STACK}", style=discord.ButtonStyle.secondary, disabled=self.qty >= MAX_STACK, row=2)
        mx.callback = self.on_qty_max  # type: ignore[assignment]
        self.add_item(mx)

        # Row 3: Action Buttons
        row_act = 3
        if self.search_view.island_name:
            drop_btn = discord.ui.Button(label=f"Drop Now (×{self.qty})", emoji="⚡", style=discord.ButtonStyle.success, row=row_act)
            drop_btn.callback = self.on_drop_now  # type: ignore[assignment]
            self.add_item(drop_btn)

            cart_label = "Update Cart" if self.is_in_cart else "Add to Cart"
            cart_btn = discord.ui.Button(label=cart_label, emoji="🛒", style=discord.ButtonStyle.primary, row=row_act)
            cart_btn.callback = self.on_add_to_cart  # type: ignore[assignment]
            self.add_item(cart_btn)

        back_btn = discord.ui.Button(label="Back to Results", emoji="🔙", style=discord.ButtonStyle.secondary, row=row_act)
        back_btn.callback = self.on_back_to_search  # type: ignore[assignment]
        self.add_item(back_btn)

        # Row 4: Tools
        row_tools = 4
        hex_btn = discord.ui.Button(label="Custom Hex", emoji="#️⃣", style=discord.ButtonStyle.secondary, row=row_tools)
        hex_btn.callback = self.on_custom_hex  # type: ignore[assignment]
        self.add_item(hex_btn)

        if self.hex_override:
            reset_btn = discord.ui.Button(label="Reset Hex", emoji="↩️", style=discord.ButtonStyle.secondary, row=row_tools)
            reset_btn.callback = self.on_reset_hex  # type: ignore[assignment]
            self.add_item(reset_btn)

        if self.is_in_cart:
            rm_btn = discord.ui.Button(label="Remove from Cart", emoji="🗑️", style=discord.ButtonStyle.danger, row=row_tools)
            rm_btn.callback = self.on_remove_from_cart  # type: ignore[assignment]
            self.add_item(rm_btn)

    def build_embed(self, notice: str | None = None) -> discord.Embed:
        it = self.current_item
        cur = self.current_variant
        lines = [
            f"**Item:** {it.name}",
            f"**Category:** `{it.category}`" + (" *(DIY Recipe)*" if it.diy else ""),
        ]
        if cur:
            lines.append(f"**Selected Color:** **{cur.body}**")
            if len(self.patterns) > 1:
                lines.append(f"**Pattern:** **{cur.pattern or _NO_PATTERN_LABEL}**")
        lines.append(f"**Quantity:** **×{self.qty}** (Stack size: {it.stack_size or 1})")

        hex_line = f"**Drop Hex:** `{self.drop_hex}`"
        if self.hex_override:
            hex_line += f" *(Custom override — catalog hex is `{it.to_hex() or '—'}`)*"
        lines.append(hex_line)

        if self.search_view.island_name:
            lines.append(f"🏝️ **Island:** [{self.search_view.island_name}]({_island_url(self.search_view.island_name)})")

        if self.is_in_cart:
            lines.append("🛒 *This item is in your drop cart.*")

        if self.search_view.island_name:
            lines.append("\n• **⚡ Drop Now**: Drops immediately near you on the island.")
            lines.append("• **🛒 Add to Cart**: Keeps this item in your cart while you browse.")

        desc = "\n".join(lines)
        if notice:
            desc = f"{notice}\n\n{desc}"

        embed = discord.Embed(
            title=f"📦 {it.display_name}",
            description=desc,
            color=PANEL_EMBED_COLOR,
        )
        if it.image_url:
            embed.set_thumbnail(url=it.image_url)

        if len(self.bodies) > 1:
            embed.add_field(
                name=f"🎨 Available Colors ({len(self.bodies)})",
                value=" · ".join(f"**▸{b}**" if cur and b == cur.body else b for b in self.bodies)[:1024],
                inline=False,
            )
        if len(self.patterns) > 1:
            embed.add_field(
                name=f"🖌️ Available Patterns ({len(self.patterns)})",
                value=" · ".join(
                    f"**▸{p or _NO_PATTERN_LABEL}**" if cur and p == cur.pattern else (p or _NO_PATTERN_LABEL)
                    for p in self.patterns
                )[:1024],
                inline=False,
            )

        n_cart = len(self.search_view.cart)
        embed.set_footer(
            text=f"{PANEL_FOOTER_TEXT} • Cart: {n_cart}/{MAX_DROP_SLOTS} slots",
            icon_url=PANEL_FOOTER_ICON,
        )
        return embed

    async def refresh(self, interaction: discord.Interaction, notice: str | None = None) -> None:
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_embed(notice), view=self)

    async def on_body_select(self, interaction: discord.Interaction) -> None:
        val = (interaction.data or {}).get("values", ["0"])[0]
        bodies = self.bodies
        try:
            body = bodies[int(val)]
        except (ValueError, IndexError):
            await interaction.response.defer()
            return
        cur = self.current_variant
        variant = self._find(body, cur.pattern if cur else "")
        if variant:
            self.current_variant = variant
            self.current_item = variant.item
            self.hex_override = None
            if self.cart_key and self.cart_key in self.search_view.cart:
                self.search_view.cart.pop(self.cart_key, None)
                new_key = _cart_key(self.current_item)
                self.search_view.cart[new_key] = _CartEntry(self.current_item, self.qty, None)
                self.cart_key = new_key
            await self.refresh(interaction, f"🎨 Color changed to **{variant.body}**.")
        else:
            await interaction.response.defer()

    async def on_pattern_select(self, interaction: discord.Interaction) -> None:
        val = (interaction.data or {}).get("values", ["0"])[0]
        patterns = self.patterns
        try:
            pattern = patterns[int(val)]
        except (ValueError, IndexError):
            await interaction.response.defer()
            return
        cur = self.current_variant
        body = cur.body if cur else ""
        variant = next((v for v in self.variants if v.body == body and v.pattern == pattern), None) \
            or next((v for v in self.variants if v.pattern == pattern), None)
        if variant:
            self.current_variant = variant
            self.current_item = variant.item
            self.hex_override = None
            if self.cart_key and self.cart_key in self.search_view.cart:
                self.search_view.cart.pop(self.cart_key, None)
                new_key = _cart_key(self.current_item)
                self.search_view.cart[new_key] = _CartEntry(self.current_item, self.qty, None)
                self.cart_key = new_key
            pat_name = variant.pattern or _NO_PATTERN_LABEL
            await self.refresh(interaction, f"🖌️ Pattern changed to **{pat_name}**.")
        else:
            await interaction.response.defer()

    async def on_minus(self, interaction: discord.Interaction) -> None:
        self.qty = max(1, self.qty - 1)
        if self.cart_key and self.cart_key in self.search_view.cart:
            self.search_view.cart[self.cart_key].qty = self.qty
        await self.refresh(interaction)

    async def on_plus(self, interaction: discord.Interaction) -> None:
        self.qty = min(MAX_STACK, self.qty + 1)
        if self.cart_key and self.cart_key in self.search_view.cart:
            self.search_view.cart[self.cart_key].qty = self.qty
        await self.refresh(interaction)

    async def on_qty_one(self, interaction: discord.Interaction) -> None:
        self.qty = 1
        if self.cart_key and self.cart_key in self.search_view.cart:
            self.search_view.cart[self.cart_key].qty = 1
        await self.refresh(interaction)

    async def on_qty_max(self, interaction: discord.Interaction) -> None:
        self.qty = MAX_STACK
        if self.cart_key and self.cart_key in self.search_view.cart:
            self.search_view.cart[self.cart_key].qty = MAX_STACK
        await self.refresh(interaction)

    async def on_drop_now(self, interaction: discord.Interaction) -> None:
        if not self.search_view.island_name:
            await interaction.response.defer()
            return
        await interaction.response.defer(ephemeral=True)
        summary = f"{self.current_item.display_name} ×{self.qty}"
        drop_entries = [(self.drop_hex, self.qty)]
        embed, ok = await _perform_drop(
            interaction.user,
            self.search_view.island_name,
            drop_entries,
            heading="⚡ Quick Drop Queued",
            contents=summary,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def on_add_to_cart(self, interaction: discord.Interaction) -> None:
        cart = self.search_view.cart
        new_key = _cart_key(self.current_item)
        if self.cart_key and self.cart_key in cart and self.cart_key != new_key:
            cart.pop(self.cart_key, None)

        if len(cart) >= MAX_DROP_SLOTS and new_key not in cart:
            await self.refresh(interaction, f"⚠️ Cart is full ({MAX_DROP_SLOTS}/{MAX_DROP_SLOTS}). Drop or clear first.")
            return

        cart[new_key] = _CartEntry(self.current_item, self.qty, self.hex_override)
        self.cart_key = new_key
        notice = f"✅ Added **{self.current_item.display_name} ×{self.qty}** to your cart! ({len(cart)}/{MAX_DROP_SLOTS} slots)"
        await self.refresh(interaction, notice)

    async def on_back_to_search(self, interaction: discord.Interaction) -> None:
        self.stop()
        self.search_view._rebuild()
        await interaction.response.edit_message(embed=self.search_view.build_search_embed(), view=self.search_view)

    async def on_custom_hex(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(ItemHexModal(self))

    async def on_reset_hex(self, interaction: discord.Interaction) -> None:
        self.hex_override = None
        if self.cart_key and self.cart_key in self.search_view.cart:
            self.search_view.cart[self.cart_key].hex_override = None
        await self.refresh(interaction, "↩️ Reset to catalog hex.")

    async def on_remove_from_cart(self, interaction: discord.Interaction) -> None:
        cart = self.search_view.cart
        if self.cart_key and self.cart_key in cart:
            cart.pop(self.cart_key, None)
        elif self.current_key in cart:
            cart.pop(self.current_key, None)
        self.cart_key = None
        await self.refresh(interaction, f"🗑️ Removed **{self.current_item.display_name}** from cart.")


class CartManagerView(_OwnedView):
    """Clean Cart overview: lists all items, select one to edit/customize, or drop cart."""

    def __init__(self, search_view: "CatalogSearchView"):
        super().__init__(user_id=search_view.user_id, timeout=300)
        self.search_view = search_view
        self._rebuild()

    def _rebuild(self) -> None:
        self.clear_items()
        cart = self.search_view.cart

        if cart:
            opts = []
            for k, e in list(cart.items())[:25]:
                n_colors = len(_item_variants(e.item))
                desc = f"×{e.qty} • Hex: {e.drop_hex}"
                opts.append(
                    discord.SelectOption(
                        label=e.item.display_name[:100],
                        description=desc[:100],
                        value=k[:100],
                        emoji="🎨" if n_colors else "📦",
                    )
                )
            sel = discord.ui.Select(
                placeholder="Tap an item to change color, quantity, or remove…",
                options=opts,
                row=0,
            )
            sel.callback = self.on_select_item  # type: ignore[assignment]
            self.add_item(sel)

        n = len(cart)
        drop_btn = discord.ui.Button(label=f"Drop Cart ({n})", emoji="📦", style=discord.ButtonStyle.success, disabled=n == 0, row=1)
        drop_btn.callback = self.on_drop_cart  # type: ignore[assignment]
        self.add_item(drop_btn)

        clear_btn = discord.ui.Button(label="Clear Cart", emoji="🗑️", style=discord.ButtonStyle.danger, disabled=n == 0, row=1)
        clear_btn.callback = self.on_clear_cart  # type: ignore[assignment]
        self.add_item(clear_btn)

        back_btn = discord.ui.Button(label="Back to Search", emoji="🔙", style=discord.ButtonStyle.secondary, row=1)
        back_btn.callback = self.on_back  # type: ignore[assignment]
        self.add_item(back_btn)

    def build_embed(self, notice: str | None = None) -> discord.Embed:
        cart = self.search_view.cart
        lines = []
        if not cart:
            lines.append("*Your drop cart is empty. Pick items from the search catalog to add them here.*")
        else:
            for idx, (k, e) in enumerate(cart.items(), 1):
                custom_tag = " `(custom hex)`" if e.hex_override else ""
                lines.append(f"`{idx:02d}.` **{e.item.display_name}** ×{e.qty} — `{e.drop_hex}`{custom_tag}")

        totals, warnings = _cart_summary(cart)
        if totals:
            lines.append(f"\n{totals}")
            lines.extend(warnings)

        if self.search_view.island_name:
            lines.append(f"\n🏝️ **Target island:** [{self.search_view.island_name}]({_island_url(self.search_view.island_name)})")

        desc = "\n".join(lines)
        if notice:
            desc = f"{notice}\n\n{desc}"

        embed = discord.Embed(
            title=f"🛒 Drop Cart ({len(cart)}/{MAX_DROP_SLOTS} Slots)",
            description=desc,
            color=PANEL_EMBED_COLOR,
        )
        embed.set_footer(text=f"{PANEL_FOOTER_TEXT} • Tap an item above to customize", icon_url=PANEL_FOOTER_ICON)
        return embed

    async def on_select_item(self, interaction: discord.Interaction) -> None:
        values = (interaction.data or {}).get("values", [])
        if not values or values[0] not in self.search_view.cart:
            await interaction.response.defer()
            return
        key = values[0]
        entry = self.search_view.cart[key]
        view = ItemDetailView(
            self.search_view,
            entry.item,
            cart_key=key,
            initial_qty=entry.qty,
            initial_hex_override=entry.hex_override,
        )
        view._orig_interaction = self.search_view._orig_interaction
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    async def on_drop_cart(self, interaction: discord.Interaction) -> None:
        await self.search_view.on_drop_cart(interaction)

    async def on_clear_cart(self, interaction: discord.Interaction) -> None:
        self.search_view.cart.clear()
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_embed("🗑️ Cart cleared."), view=self)

    async def on_back(self, interaction: discord.Interaction) -> None:
        self.stop()
        self.search_view._rebuild()
        await interaction.response.edit_message(embed=self.search_view.build_search_embed(), view=self.search_view)


async def _show_catalog(
    interaction: discord.Interaction,
    island: str | None,
    query: str,
    parent: "CatalogSearchView | None" = None,
) -> None:
    """Run a search and show it: edits `parent` in place if given, else sends a new ephemeral message."""
    if not interaction.response.is_done():
        if parent is not None:
            await interaction.response.defer()
        else:
            await interaction.response.defer(ephemeral=True, thinking=True)

    try:
        items, villagers = await asyncio.to_thread(_search_catalog, query)
    except Exception:
        logger.exception("[DropBot] Catalog search failed for %r", query)
        await _reply(interaction, embed=_err_embed("The catalog search failed. Please try again."))
        return

    is_staff = False
    if isinstance(interaction.user, discord.Member):
        perms = interaction.user.guild_permissions
        is_staff = bool(perms.administrator or perms.manage_guild or perms.manage_messages)
    elif interaction.permissions and interaction.permissions.manage_messages:
        is_staff = True

    if parent is not None:
        parent.is_staff = is_staff
        parent.apply_search(query, items, villagers)
        await interaction.edit_original_response(embed=parent.build_search_embed(), view=parent)
        return

    view = CatalogSearchView(
        island_name=island,
        query=query,
        items=items,
        villagers=villagers,
        user_id=interaction.user.id,
        is_staff=is_staff,
    )
    view._orig_interaction = interaction
    notice = None
    if island:
        view._presence_status = await _presence_line(interaction.user, island)
        saved = await asyncio.to_thread(_load_cart_rows, interaction.user.id, island)
        if saved:
            restored = await asyncio.to_thread(_restore_cart_entries, saved)
            for key, entry in restored:
                view.cart.adopt(key, entry)
            if restored:
                view._rebuild()
                notice = f"🛒 Restored **{len(restored)}** item(s) from your saved cart."
    view.message = await interaction.followup.send(embed=view.build_search_embed(notice), view=view, ephemeral=True)


class CatalogSearchModal(discord.ui.Modal, title="🔍 Search ACNH Catalog"):
    search_query = discord.ui.TextInput(
        label="Search Keyword (Items & Villagers)",
        placeholder="e.g. royal crown, gold, Raymond, painting, diy",
        required=True,
        max_length=60,
    )

    def __init__(self, island_name: str | None = None, parent: "CatalogSearchView | None" = None):
        super().__init__()
        self.island_name = island_name
        self.parent_view = parent

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await _show_catalog(interaction, self.island_name, self.search_query.value.strip(), self.parent_view)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logger.error("[DropBot] CatalogSearchModal error: %s", error, exc_info=error)
        await _reply(interaction, "❌ Something went wrong while searching the catalog.")


class CartEditModal(discord.ui.Modal, title="🛒 Edit Drop Cart"):
    """Edit quantities in bulk. One 'N. Item: quantity' per line; 0 removes the item."""

    def __init__(self, parent: "CatalogSearchView"):
        super().__init__()
        self.parent_view = parent
        self._keys: list[str] = list(parent.cart.keys())
        lines = []
        for idx, key in enumerate(self._keys, 1):
            e = parent.cart[key]
            lines.append(f"{idx}. {e.item.display_name}: {e.qty}")
        self.lines = discord.ui.TextInput(
            label=f"N. Item: quantity  (1–{MAX_STACK}, 0 removes)",
            style=discord.TextStyle.paragraph,
            default="\n".join(lines)[:4000],
            max_length=4000,
            required=True,
        )
        self.add_item(self.lines)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cart = self.parent_view.cart
        updates: dict[str, int] = {}
        for raw in self.lines.value.splitlines():
            raw = raw.strip()
            if not raw:
                continue
            name_part, sep, qty_s = raw.rpartition(":")
            qty_s = qty_s.strip()
            if not sep or not qty_s.isdigit():
                await _reply(interaction, embed=_err_embed(f"I couldn't read “{raw[:50]}”. Use `N. Item: quantity`."))
                return
            qty = int(qty_s)
            if qty > MAX_STACK:
                await _reply(interaction, embed=_err_embed(f"Quantity can't exceed {MAX_STACK}."))
                return
            key = None
            clean_name = name_part.strip()
            m = re.match(r"^(\d+)\.\s*(.*)$", clean_name)
            if m:
                line_idx = int(m.group(1)) - 1
                if 0 <= line_idx < len(self._keys):
                    key = self._keys[line_idx]
                target_name = m.group(2).strip().lower()
            else:
                target_name = clean_name.lower()
            if not key:
                for k, e in cart.items():
                    if e.item.display_name.lower() == target_name:
                        key = k
                        break
            if not key or key not in cart:
                await _reply(interaction, embed=_err_embed(f"I couldn't find “{clean_name[:40]}” in your cart."))
                return
            updates[key] = qty

        for key, qty in updates.items():
            if qty == 0:
                cart.pop(key, None)
            else:
                cart[key].qty = qty

        self.parent_view._rebuild()
        if hasattr(self.parent_view, "build_search_embed"):
            await interaction.response.edit_message(
                embed=self.parent_view.build_search_embed("✏️ Cart updated."), view=self.parent_view
            )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logger.error("[DropBot] CartEditModal error: %s", error, exc_info=error)
        await _reply(interaction, "❌ Something went wrong. Please try again.")


class CatalogSearchView(_OwnedView):
    """
    Streamlined Catalog Search View:
    - Row 0: Tap an item to open ItemDetailView (color, quantity, drop now, add to cart)
    - Row 1: Category filter OR Villagers select (if matching query)
    - Row 2: Navigation (◀, 1/N, ▶, Search, Close)
    - Row 3: Cart bar (Drop Cart, View Cart, Clear) when cart has items
    """

    not_owner_msg = "This search menu is not yours."

    def __init__(
        self,
        island_name: str | None = None,
        query: str = "",
        items: list[CatalogItem] | None = None,
        villagers: list[CatalogVillager] | None = None,
        user_id: int | None = None,
        page_size: int = 10,
        is_staff: bool = False,
    ):
        super().__init__(user_id=user_id or 0, timeout=300)
        self.island_name = island_name
        self.query = query
        self.all_items: list[CatalogItem] = list(items or [])
        self.items: list[CatalogItem] = self.all_items
        self.villagers: list[CatalogVillager] = list(villagers or [])
        self._items_by_key: dict[str, CatalogItem] = {}
        for idx, i in enumerate(self.all_items):
            k = _cart_key(i) or f"item_{idx}"
            self._items_by_key[k] = i
        self.page_size = page_size
        self.page = 0
        self.category: str | None = None
        self.type_filter: str = "all"  # 'all', 'items', 'recipes'
        self.cart: _PersistentCart = _PersistentCart(user_id or 0, island_name)
        self.message: discord.Message | None = None
        self._busy = False
        self.is_staff = is_staff
        self._presence_status: str | None = None
        self._apply_filters()
        self._rebuild()

    # ── state ──
    @property
    def total_pages(self) -> int:
        return max(1, math.ceil(len(self.items) / self.page_size))

    def page_items(self) -> list[CatalogItem]:
        start = self.page * self.page_size
        return self.items[start : start + self.page_size]

    def _apply_filters(self) -> None:
        filtered = self.all_items
        if self.category:
            filtered = [i for i in filtered if i.category == self.category]
        if self.type_filter == "items":
            filtered = [i for i in filtered if not i.diy]
        elif self.type_filter == "recipes":
            filtered = [i for i in filtered if i.diy]
        self.items = filtered
        self.page = 0

    def apply_search(self, query: str, items: list[CatalogItem], villagers: list[CatalogVillager]) -> None:
        self.query = query
        self.all_items = list(items)
        self.villagers = list(villagers)
        self._items_by_key = {}
        for idx, i in enumerate(self.all_items):
            k = _cart_key(i) or f"item_{idx}"
            self._items_by_key[k] = i
        self.category = None
        self._apply_filters()
        self._rebuild()

    def _set_category(self, category: str | None) -> None:
        self.category = category
        self._apply_filters()
        self._rebuild()

    # ── components ──
    def _rebuild(self) -> None:
        self.clear_items()
        page_items = self.page_items()
        counts = Counter(i.category for i in self.all_items if i.category)

        # Row 0: Items dropdown
        if page_items:
            options: list[discord.SelectOption] = []
            seen_values: set[str] = set()
            for idx, it in enumerate(page_items):
                bits: list[str] = []
                key = _cart_key(it)
                if not key or key in seen_values:
                    key = f"{key or 'item'}_{self.page}_{idx}"
                    self._items_by_key[key] = it
                seen_values.add(key)

                if key in self.cart or _cart_key(it) in self.cart:
                    bits.append("✅ In cart")
                bits.append(str(it.category or "Item"))
                bits.append(f"×{_default_qty(it)}")
                n_colors = len(_item_variants(it))
                if n_colors:
                    bits.append(f"🎨 {n_colors} colors")
                options.append(
                    discord.SelectOption(
                        label=it.display_name[:100],
                        description=" • ".join(bits)[:100],
                        value=key[:100],
                        emoji="📜" if it.diy else "📦",
                    )
                )
            placeholder = (
                "📦 Select item to customize color, qty, or drop…"
                if self.island_name
                else f"Select item to inspect (page {self.page + 1}/{self.total_pages})…"
            )
            item_select = discord.ui.Select(
                placeholder=placeholder[:100],
                min_values=1,
                max_values=min(len(options), MAX_DROP_SLOTS),
                options=options[:25],
                row=0,
            )
            item_select.callback = self.on_item_select  # type: ignore[assignment]
            self.add_item(item_select)

        # Row 1: Villagers (page 0) OR Category filter
        if self.villagers and self.page == 0:
            v_options = []
            seen_v: set[str] = set()
            for idx, v in enumerate(self.villagers[:10]):
                val = f"v_{v.villager_id}"
                if val in seen_v:
                    val = f"{val}_{idx}"
                seen_v.add(val)
                v_options.append(
                    discord.SelectOption(
                        label=v.display_name[:100],
                        description=f"ID: {v.villager_id} • {v.personality}"[:100],
                        value=val[:100],
                        emoji="🐱",
                    )
                )
            villager_select = discord.ui.Select(
                placeholder=(f"🐱 Inject a villager into {self.island_name}…" if self.island_name
                             else "Select villager to inspect…")[:100],
                min_values=1,
                max_values=1,
                options=v_options[:25],
                row=1,
            )
            villager_select.callback = self.on_villager_select  # type: ignore[assignment]
            self.add_item(villager_select)
        elif len(counts) > 1:
            cat_options = [
                discord.SelectOption(
                    label=f"All categories ({len(self.all_items)})", value="__all__", default=self.category is None
                )
            ]
            for cat, n in counts.most_common(24):
                cat_options.append(
                    discord.SelectOption(
                        label=str(cat)[:100], description=f"{n} item(s)", value=str(cat)[:100],
                        default=(cat == self.category),
                    )
                )
            cat_select = discord.ui.Select(
                placeholder="🗂️ Filter by category…", min_values=1, max_values=1, options=cat_options[:25], row=1
            )
            cat_select.callback = self.on_category_select  # type: ignore[assignment]
            self.add_item(cat_select)

        # Row 2: Navigation & Search
        prev_btn = discord.ui.Button(label="◀", style=discord.ButtonStyle.secondary, disabled=self.page <= 0, row=2)
        prev_btn.callback = self.on_prev_page  # type: ignore[assignment]
        self.add_item(prev_btn)

        page_btn = discord.ui.Button(
            label=f"{self.page + 1}/{self.total_pages}", style=discord.ButtonStyle.secondary, disabled=True, row=2
        )
        self.add_item(page_btn)

        next_btn = discord.ui.Button(
            label="▶", style=discord.ButtonStyle.secondary, disabled=self.page >= self.total_pages - 1, row=2
        )
        next_btn.callback = self.on_next_page  # type: ignore[assignment]
        self.add_item(next_btn)

        search_btn = discord.ui.Button(label="Search", emoji="🔍", style=discord.ButtonStyle.primary, row=2)
        search_btn.callback = self.on_search_button  # type: ignore[assignment]
        self.add_item(search_btn)

        close_btn = discord.ui.Button(label="Close", emoji="✖", style=discord.ButtonStyle.secondary, row=2)
        close_btn.callback = self.on_close  # type: ignore[assignment]
        self.add_item(close_btn)

        # Row 3: Smart filters & Pockets & Last Cart
        type_labels = {
            "all": ("Type: All", "🏷️"),
            "items": ("Type: Items", "📦"),
            "recipes": ("Type: DIYs", "📜"),
        }
        lbl, emj = type_labels.get(self.type_filter, ("Type: All", "🏷️"))
        type_btn = discord.ui.Button(label=lbl, emoji=emj, style=discord.ButtonStyle.secondary, row=3)
        type_btn.callback = self.on_toggle_type_filter  # type: ignore[assignment]
        self.add_item(type_btn)

        series_btn = discord.ui.Button(label="Series", emoji="🌟", style=discord.ButtonStyle.secondary, row=3)
        series_btn.callback = self.on_open_series_filter  # type: ignore[assignment]
        self.add_item(series_btn)

        if self.island_name:
            pockets_btn = discord.ui.Button(label="Pockets", emoji="🎒", style=discord.ButtonStyle.secondary, row=3)
            pockets_btn.callback = self.on_open_saved_pockets  # type: ignore[assignment]
            self.add_item(pockets_btn)

            if not self.cart and _get_user_last_drop(self.user_id):
                last_cart_btn = discord.ui.Button(label="Load Last Cart", emoji="🔁", style=discord.ButtonStyle.secondary, row=3)
                last_cart_btn.callback = self.on_load_last_cart  # type: ignore[assignment]
                self.add_item(last_cart_btn)

        # Row 4: Cart Actions (Only shown when cart has items)
        if self.island_name and self.cart:
            n = len(self.cart)
            drop_btn = discord.ui.Button(
                label=f"Drop Cart ({n})", emoji="📦", style=discord.ButtonStyle.success, row=4
            )
            drop_btn.callback = self.on_drop_cart  # type: ignore[assignment]
            self.add_item(drop_btn)

            view_cart_btn = discord.ui.Button(
                label="View Cart", emoji="🛒", style=discord.ButtonStyle.primary, row=4
            )
            view_cart_btn.callback = self.on_open_cart_manager  # type: ignore[assignment]
            self.add_item(view_cart_btn)

            clear_btn = discord.ui.Button(
                label="Clear", emoji="🗑️", style=discord.ButtonStyle.danger, row=4
            )
            clear_btn.callback = self.on_clear_cart  # type: ignore[assignment]
            self.add_item(clear_btn)

    async def on_toggle_type_filter(self, interaction: discord.Interaction, button: Any = None) -> None:
        if self.type_filter == "all":
            self.type_filter = "items"
        elif self.type_filter == "items":
            self.type_filter = "recipes"
        else:
            self.type_filter = "all"
        self._apply_filters()
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_search_embed(), view=self)

    async def on_open_series_filter(self, interaction: discord.Interaction, button: Any = None) -> None:
        view = SeriesFilterView(self)
        view._orig_interaction = getattr(self, "_orig_interaction", None)
        embed = discord.Embed(
            title="🌟 Popular Collections & Series",
            description="Select a popular ACNH series from the dropdown to instantly search matching items and DIY recipes.",
            color=PANEL_EMBED_COLOR,
        )
        embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
        await interaction.response.edit_message(embed=embed, view=_adopt(view, interaction))

    async def on_open_saved_pockets(self, interaction: discord.Interaction, button: Any = None) -> None:
        view = SavedPocketPickerView(self.island_name or "Island", self.user_id, parent_view=self)
        view._orig_interaction = getattr(self, "_orig_interaction", None)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    async def on_load_last_cart(self, interaction: discord.Interaction, button: Any = None) -> None:
        last = _get_user_last_drop(self.user_id)
        if not last:
            await interaction.response.send_message("No previous drop found to restore.", ephemeral=True)
            return

        payload = last.get("payload") or ""
        parsed, _ = parse_drop_items(payload)
        catalog = ACNHCatalog.get_instance()
        loaded_count = 0
        if parsed:
            for name_or_hex, qty in parsed:
                it = catalog.get_item(name_or_hex)
                if not it:
                    it = CatalogItem(
                        name=name_or_hex,
                        category="Restored",
                        internal_id=name_or_hex if len(name_or_hex) in (4, 16) else "",
                    )
                k = _cart_key(it)
                self.cart[k] = _CartEntry(
                    item=it,
                    qty=qty,
                    hex_override=name_or_hex if len(name_or_hex) in (4, 16) else None,
                )
                loaded_count += 1
                if len(self.cart) >= MAX_DROP_SLOTS:
                    break

        self._rebuild()
        msg = f"✅ Restored **{loaded_count}** item(s) from your previous order!" if loaded_count else "Could not restore items."
        await interaction.response.edit_message(
            embed=self.build_search_embed(notice=msg),
            view=self,
        )

    # ── embed ──
    def build_search_embed(self, notice: str | None = None) -> discord.Embed:
        page_items = self.page_items()
        desc = f"Found **{len(self.all_items)}** items and **{len(self.villagers)}** villagers."
        filter_parts = []
        if self.category:
            filter_parts.append(f"🗂️ {self.category}")
        if self.type_filter == "items":
            filter_parts.append("📦 Items Only")
        elif self.type_filter == "recipes":
            filter_parts.append("📜 DIY Recipes Only")
        if filter_parts:
            desc += f"\n**Filter:** {' · '.join(filter_parts)} ({len(self.items)} shown)"
        desc += f"\n**Page:** `{self.page + 1} / {self.total_pages}`"
        if self.island_name:
            desc += f"\n🏝️ **Target island:** [{self.island_name}]({_island_url(self.island_name)})"
            if self._presence_status:
                desc += f"\n📍 **Presence:** {self._presence_status}"
            desc += "\n*Tap an item below to pick color, stack size, and drop or add to cart.*"
        else:
            desc += "\n*Select an item below to inspect variations and hex codes.*"
        if notice:
            desc = f"{notice}\n\n{desc}"

        embed = discord.Embed(title=f"🔍 Catalog Search: '{self.query or 'All'}'", description=desc, color=PANEL_EMBED_COLOR)

        if self.villagers and self.villagers[0].photo_url and self.page == 0:
            embed.set_thumbnail(url=self.villagers[0].photo_url)
        elif page_items and page_items[0].image_url:
            embed.set_thumbnail(url=page_items[0].image_url)

        if page_items:
            lines = []
            start_num = self.page * self.page_size + 1
            for i, it in enumerate(page_items, start_num):
                cart_mark = "🛒 " if _cart_key(it) in self.cart else ""
                diy_badge = " *(DIY)*" if it.diy else ""
                n_colors = len(_item_variants(it))
                color_badge = f" • *{n_colors} colors*" if n_colors > 1 else ""
                hex_code = it.to_hex()
                hex_part = f" • `{hex_code}`" if (hex_code and self.is_staff) else ""
                lines.append(f"`{i:02d}.` {cart_mark}**{it.display_name}**{diy_badge} — `{it.category}`{color_badge}{hex_part}")

            chunk: list[str] = []
            c_len = 0
            first = True
            for line in lines + [None]:  # type: ignore[list-item]
                if line is None or c_len + len(line) + 1 > 950:
                    if chunk:
                        embed.add_field(
                            name=f"📦 Matching Items (Page {self.page + 1}/{self.total_pages})" if first else "📦 Items (cont.)",
                            value="\n".join(chunk),
                            inline=False,
                        )
                        first = False
                    chunk, c_len = ([line], len(line)) if line else ([], 0)
                else:
                    chunk.append(line)
                    c_len += len(line) + 1

        if self.villagers and self.page == 0:
            v_lines = [
                f"• **{v.name}** (`ID: {v.villager_id}`) — *{v.species} / {v.personality}*" for v in self.villagers[:6]
            ]
            embed.add_field(name="🐱 Matching Villagers", value="\n".join(v_lines)[:1024], inline=False)

        if self.island_name and self.cart:
            cart_summary = ", ".join(
                f"**{e.item.display_name}** ×{e.qty}" + (f" `#{e.hex_override}`" if e.hex_override else "")
                for e in self.cart.values()
            )
            totals, warnings = _cart_summary(self.cart)
            extra = "\n".join([totals, *warnings[:2]])
            embed.add_field(
                name=f"🛒 Drop Cart ({len(self.cart)}/{MAX_DROP_SLOTS} slots)",
                value=(
                    f"{cart_summary[:600]}\n{extra}\n"
                    "*Press **Drop Cart** to drop all, or **View Cart** to customize.*"
                )[:1024],
                inline=False,
            )

        if not self.all_items and not self.villagers:
            embed.description = (
                f"❌ No items or villagers found matching **{self.query}**.\n\n"
                "**Suggestions:**\n"
                "• Try a shorter keyword or check spelling\n"
                "• Filter by category or search with a broader term\n"
                "• For villagers, try searching with the **Inject Villager** button"
            )

        embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
        return embed

    async def _refresh(self, interaction: discord.Interaction, notice: str | None = None) -> None:
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_search_embed(notice), view=self)

    # ── navigation / filter ──
    async def on_prev_page(self, interaction: discord.Interaction) -> None:
        self.page = max(0, self.page - 1)
        await self._refresh(interaction)

    async def on_next_page(self, interaction: discord.Interaction) -> None:
        self.page = min(self.total_pages - 1, self.page + 1)
        await self._refresh(interaction)

    async def on_category_select(self, interaction: discord.Interaction) -> None:
        val = (interaction.data or {}).get("values", ["__all__"])[0]
        self._set_category(None if val == "__all__" else val)
        await interaction.response.edit_message(embed=self.build_search_embed(), view=self)

    async def on_search_button(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(CatalogSearchModal(island_name=self.island_name, parent=self))

    async def on_close(self, interaction: discord.Interaction) -> None:
        self.stop()
        try:
            await interaction.response.defer()
            await interaction.delete_original_response()
        except discord.HTTPException:
            pass

    # ── item selection ──
    async def on_item_select(self, interaction: discord.Interaction) -> None:
        values = (interaction.data or {}).get("values", [])
        if not values:
            await interaction.response.defer()
            return

        chosen: list[CatalogItem] = []
        for val in values:
            it = self._items_by_key.get(val)
            if not it and val.startswith("item_"):
                try:
                    idx = int(val.replace("item_", ""))
                    if 0 <= idx < len(self.page_items()):
                        it = self.page_items()[idx]
                except ValueError:
                    pass
            if it:
                chosen.append(it)

        if not chosen:
            await interaction.response.defer()
            return

        # Single item selected → Open Item Detail Card immediately (ChoPaeng Command Builder style!)
        if len(chosen) == 1:
            it = chosen[0]
            cart_key = _cart_key(it)
            initial_qty = self.cart[cart_key].qty if cart_key in self.cart else _default_qty(it)
            initial_hex = self.cart[cart_key].hex_override if cart_key in self.cart else None
            detail_view = ItemDetailView(
                self,
                it,
                cart_key=cart_key if cart_key in self.cart else None,
                initial_qty=initial_qty,
                initial_hex_override=initial_hex,
            )
            detail_view._orig_interaction = self._orig_interaction
            await interaction.response.edit_message(embed=detail_view.build_embed(), view=detail_view)
            return

        # Multiple items selected → Batch add directly to cart
        if self.island_name:
            added, already, skipped = 0, 0, 0
            for it in chosen:
                key = _cart_key(it)
                if key in self.cart:
                    already += 1
                elif len(self.cart) >= MAX_DROP_SLOTS:
                    skipped += 1
                else:
                    self.cart[key] = _CartEntry(it, _default_qty(it))
                    added += 1
            notes = []
            if added:
                notes.append(f"🛒 Added **{added}** items to your drop cart.")
            if already:
                notes.append(f"{already} already in cart.")
            if skipped:
                notes.append(f"⚠️ Cart full — {skipped} skipped.")
            await self._refresh(interaction, " ".join(notes))
            return

        # Inspect mode with multiple selections (no island)
        embed = discord.Embed(
            title=f"📦 Selected Items ({len(chosen)})",
            description="Catalog details and hex codes for your selection:",
            color=PANEL_EMBED_COLOR,
        )
        for it in chosen:
            diy_txt = " *(DIY Recipe)*" if it.diy else ""
            hex_code = it.to_hex() or it.internal_id or "N/A"
            value = f"• Category: `{it.category}`\n• Hex Code: `{hex_code}`\n• Stack Size: `{it.stack_size}`"
            variants = _item_variants(it)
            if variants:
                colors = ", ".join(f"{v.item.variation} `{v.item.to_hex()}`" for v in variants[:8])
                value += f"\n• Colors ({len(variants)}): {colors}"
            embed.add_field(
                name=f"{it.display_name}{diy_txt}",
                value=value[:1024],
                inline=False,
            )
        if chosen[0].image_url:
            embed.set_thumbnail(url=chosen[0].image_url)
        embed.set_footer(text="💡 Tip: Tap a single item to view its dedicated customizer.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── cart actions ──
    async def on_open_cart_manager(self, interaction: discord.Interaction) -> None:
        cart_view = CartManagerView(self)
        cart_view._orig_interaction = self._orig_interaction
        await interaction.response.edit_message(embed=cart_view.build_embed(), view=cart_view)

    async def on_clear_cart(self, interaction: discord.Interaction) -> None:
        self.cart.clear()
        await self._refresh(interaction, "🗑️ Cart cleared.")

    async def on_drop_cart(self, interaction: discord.Interaction) -> None:
        if not self.cart or not self.island_name or self._busy:
            await interaction.response.defer()
            return
        self._busy = True
        try:
            await interaction.response.defer()
            self._presence_status = await _presence_line(interaction.user, self.island_name)
            entries = list(self.cart.values())
            drop_entries = [(e.drop_hex, e.qty) for e in entries]
            summary = ", ".join(f"{e.item.display_name} ×{e.qty}" for e in entries)
            embed, ok = await _perform_drop(
                interaction.user,
                self.island_name,
                drop_entries,
                heading="📦 Catalog Drop Queued",
                contents=summary,
            )
            if ok:
                self.cart.clear()
            self._rebuild()
            notice = "✅ Drop queued — cart cleared." if ok else "⚠️ Drop not sent — your cart is kept."
            with contextlib.suppress(discord.HTTPException):
                await interaction.edit_original_response(embed=self.build_search_embed(notice), view=self)
            await interaction.followup.send(embed=embed, ephemeral=True)
        finally:
            self._busy = False

    # ── villagers ──
    async def on_villager_select(self, interaction: discord.Interaction) -> None:
        values = (interaction.data or {}).get("values", [])
        if not values:
            await interaction.response.defer()
            return
        val = values[0]
        v: CatalogVillager | None = None
        if val.startswith("v_"):
            vid = val[2:]
            v = next((x for x in self.villagers if str(x.villager_id) == vid), None)
        elif val.startswith("villager_"):
            try:
                idx = int(val.replace("villager_", ""))
                if 0 <= idx < len(self.villagers):
                    v = self.villagers[idx]
            except (ValueError, IndexError):
                pass

        if not v:
            await interaction.response.defer()
            return

        if self.island_name:
            if is_sanrio_villager(v.name):
                await _start_injection(interaction, self.island_name, v.name)
                return
            prompt = discord.Embed(
                title="Inject this villager?",
                description=f"**{v.name}** → **{self.island_name}** (next available plot)\n\n*Adds {v.name} to your island's next open plot.*",
                color=PANEL_EMBED_COLOR_WARN,
            )
            if v.photo_url:
                prompt.set_thumbnail(url=v.photo_url)
            prompt.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
            view = VillagerChoiceView(interaction.user.id, self.island_name, [v.name], None)
            await interaction.response.send_message(embed=prompt, view=_adopt(view, interaction), ephemeral=True)
        else:
            embed = discord.Embed(
                title=f"🐱 Villager: {v.name}",
                description=(
                    f"**Species:** {v.species}\n"
                    f"**Personality:** {v.personality}\n"
                    f"**Internal ID:** `{v.villager_id}`\n\n"
                    f"💡 To inject **{v.name}**, use `/catalog` in an island channel or the "
                    "**Search Catalog** button on an island panel."
                ),
                color=PANEL_EMBED_COLOR,
            )
            if v.photo_url:
                embed.set_thumbnail(url=v.photo_url)
            await interaction.response.send_message(embed=embed, ephemeral=True)


# ─── Interactive Drop & Villager Pickers ───────────────────────────────────────


class DropFollowupView(_OwnedView):
    """Shown after a successful drop: Drop again, New drop menu, etc."""

    def __init__(self, island_name: str, user_id: int, last_drop: str, display_name: str = ""):
        super().__init__(user_id=user_id, timeout=120)
        self.island_name = island_name
        self.last_drop = last_drop
        self.display_name = display_name or last_drop

    @discord.ui.button(label="Drop Again", emoji="🔁", style=discord.ButtonStyle.primary)
    async def on_repeat(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()
        embed, _ = await _perform_drop(
            interaction.user,
            self.island_name,
            self.last_drop,
            heading="📦 Drop queued",
            contents=self.display_name,
        )
        followup = DropFollowupView(self.island_name, self.user_id, self.last_drop, self.display_name)
        await interaction.edit_original_response(
            embed=embed,
            view=followup,
        )
        self.stop()

    @discord.ui.button(label="New Drop Menu", emoji="📦", style=discord.ButtonStyle.secondary)
    async def on_new_drop(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        picker = InteractiveDropPickerView(self.island_name, self.user_id)
        embed = await build_drop_picker_embed(self.island_name, user=interaction.user)
        await interaction.response.edit_message(embed=embed, view=_adopt(picker, interaction))
        self.stop()


class PresetDropConfirmView(_OwnedView):
    """Confirm preset item drop before submitting to prevent accidental drops on mobile."""

    def __init__(self, island_name: str, user_id: int, item_code: str, label: str, emoji: str = "📦"):
        super().__init__(user_id=user_id, timeout=90)
        self.island_name = island_name
        self.item_code = item_code
        self.label = label
        self.emoji = emoji

    @discord.ui.button(label="Drop Now", emoji="📦", style=discord.ButtonStyle.success)
    async def on_confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()
        embed, _ = await _perform_drop(
            interaction.user,
            self.island_name,
            self.item_code,
            heading="📦 Drop queued",
            contents=self.label,
        )
        await interaction.edit_original_response(
            embed=embed,
            view=DropFollowupView(self.island_name, self.user_id, self.item_code, self.label),
        )
        self.stop()

    @discord.ui.button(label="Back", emoji="🔙", style=discord.ButtonStyle.secondary)
    async def on_back(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        picker = InteractiveDropPickerView(self.island_name, self.user_id)
        embed = await build_drop_picker_embed(self.island_name, user=interaction.user)
        await interaction.response.edit_message(embed=embed, view=_adopt(picker, interaction))
        self.stop()


# ─── Popular Catalog Series & Pocket Bundles ──────────────────────────────────
# Dynamically sourced via DataManager & ACNHCatalog (no hardcoding in Cog)

def _get_popular_catalog_series() -> list[tuple[str, str, str]]:
    """Return popular series dynamically from ACNHCatalog."""
    return ACNHCatalog.get_instance().get_popular_series()


def _get_pocket_bundles() -> list[tuple[str, str, str, str]]:
    """Return official pocket bundles dynamically from DataManager and database."""
    from utils.data_manager import get_pocket_bundles
    return get_pocket_bundles(max_slots=MAX_DROP_SLOTS, connect_db_func=connect_db)


# Backward-compatible references for external tests / legacy imports
POPULAR_CATALOG_SERIES: list[tuple[str, str, str]] = _get_popular_catalog_series()


CURATED_POCKET_BUNDLES: list[tuple[str, str, str, str]] = _get_pocket_bundles()


class SeriesFilterView(_OwnedView):
    """Popular collection/series quick selector."""

    def __init__(self, search_view: "CatalogSearchView"):
        super().__init__(user_id=search_view.user_id, timeout=120)
        self.search_view = search_view

        options = [
            discord.SelectOption(
                label=label,
                description=f"Quick search for {label} items & DIYs",
                emoji=emoji,
                value=kw,
            )
            for label, kw, emoji in _get_popular_catalog_series()
        ]

        select = discord.ui.Select(
            placeholder="🌟 Select a popular series / collection…",
            options=options,
            row=0,
        )
        select.callback = self.on_select_series
        self.add_item(select)

        back_btn = discord.ui.Button(label="Back to Search", emoji="🔙", style=discord.ButtonStyle.secondary, row=1)
        back_btn.callback = self.on_back
        self.add_item(back_btn)

    async def on_select_series(self, interaction: discord.Interaction) -> None:
        vals = (interaction.data or {}).get("values", [])
        kw = vals[0] if vals else ""
        items, villagers = await asyncio.to_thread(_search_catalog, kw)
        self.search_view.apply_search(kw, items, villagers)
        await interaction.response.edit_message(embed=self.search_view.build_search_embed(), view=self.search_view)
        self.stop()

    async def on_back(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(embed=self.search_view.build_search_embed(), view=self.search_view)
        self.stop()


class SavedPocketPickerView(_OwnedView):
    """Browse and drop curated & saved pocket bundles (full inventory sets)."""

    def __init__(self, island_name: str, user_id: int, parent_view: discord.ui.View | None = None):
        super().__init__(user_id=user_id, timeout=180)
        self.island_name = island_name
        self.parent_view = parent_view
        self.bundles = _get_pocket_bundles()

        options = [
            discord.SelectOption(
                label=name[:100],
                description=desc[:100],
                emoji=emoji,
                value=f"bundle_{idx}",
            )
            for idx, (name, desc, code, emoji) in enumerate(self.bundles[:25])
        ]

        if options:
            bundle_select = discord.ui.Select(
                placeholder="🎒 Choose a Saved Pocket Bundle to drop…",
                options=options,
                row=0,
            )
            bundle_select.callback = self.on_bundle_select
            self.add_item(bundle_select)

        back_btn = discord.ui.Button(label="Back", emoji="🔙", style=discord.ButtonStyle.secondary, row=1)
        back_btn.callback = self.on_back
        self.add_item(back_btn)

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=f"🎒 Saved Pocket Bundles — {self.island_name}",
            description=(
                f"Select a pre-assembled pocket bundle to drop onto **[{self.island_name}]({_island_url(self.island_name)})**.\n\n"
                "• **Curated Sets:** Complete toolsets, crafting materials, and seasonal packs\n"
                "• **1-Click Drop:** Choose a bundle from the dropdown to review and drop\n"
                "• **Safe Drops:** Items drop near your character once verified on the island"
            ),
            color=PANEL_EMBED_COLOR,
        )
        for idx, (name, desc, code, emoji) in enumerate(self.bundles[:6]):
            sample_items = [s.strip() for s in code.split(",")[:3]]
            preview = ", ".join(sample_items)
            if len(code.split(",")) > 3:
                preview += "…"
            embed.add_field(name=f"{emoji} {name}", value=f"{desc}\n`{preview[:50]}`", inline=True)
        embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
        return embed

    async def on_bundle_select(self, interaction: discord.Interaction) -> None:
        vals = (interaction.data or {}).get("values", [])
        val = vals[0] if vals else ""
        if not val.startswith("bundle_"):
            return
        idx = int(val.replace("bundle_", ""))
        name, desc, item_code, emoji = self.bundles[idx]

        confirm_embed = discord.Embed(
            title=f"🎒 Confirm Pocket Drop — {name}",
            description=(
                f"You are about to drop **{emoji} {name}** on **[{self.island_name}]({_island_url(self.island_name)})**.\n\n"
                f"**Contents:**\n```{item_code[:300]}```\n"
                "👉 **Make sure you are standing outside the airport on the island!**"
            ),
            color=PANEL_EMBED_COLOR,
        )
        confirm_embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
        confirm_view = PresetDropConfirmView(self.island_name, self.user_id, item_code, name, emoji)
        await interaction.response.edit_message(embed=confirm_embed, view=_adopt(confirm_view, interaction))

    async def on_back(self, interaction: discord.Interaction) -> None:
        if self.parent_view and hasattr(self.parent_view, "build_search_embed"):
            await interaction.response.edit_message(embed=self.parent_view.build_search_embed(), view=self.parent_view)
        elif self.parent_view:
            embed = await build_drop_picker_embed(self.island_name, user=interaction.user)
            await interaction.response.edit_message(embed=embed, view=_adopt(self.parent_view, interaction))
        else:
            picker = InteractiveDropPickerView(self.island_name, self.user_id)
            embed = await build_drop_picker_embed(self.island_name, user=interaction.user)
            await interaction.response.edit_message(embed=embed, view=_adopt(picker, interaction))
        self.stop()


class InteractiveDropPickerView(_OwnedView):
    """Interactive item drop picker: presets dropdown, search catalog, browse all, or custom text."""

    def __init__(self, island_name: str, user_id: int):
        super().__init__(user_id=user_id, timeout=180)
        self.island_name = island_name

        catalog = ACNHCatalog.get_instance()
        presets_data = catalog.get_drop_presets()
        if not presets_data:
            from utils.acnh_catalog import POPULAR_DROP_PRESETS
            presets_data = list(POPULAR_DROP_PRESETS)
        preset_options = [
            discord.SelectOption(
                label=label[:100],
                description=desc[:100],
                emoji=emoji,
                value=item_code[:100],
            )
            for label, desc, item_code, emoji in presets_data
        ][:25]
        if preset_options:
            preset_select = discord.ui.Select(
                placeholder="📦 Select an item preset to drop…",
                options=preset_options,
                row=0,
            )
            preset_select.callback = self._on_preset_select
            self.add_item(preset_select)

        # Row 1: Search, Pockets, Re-drop
        search_btn = discord.ui.Button(label="Search Catalog", emoji="🔍", style=discord.ButtonStyle.primary, row=1)
        search_btn.callback = self._on_search_catalog
        self.add_item(search_btn)

        pockets_btn = discord.ui.Button(label="Saved Pockets", emoji="🎒", style=discord.ButtonStyle.primary, row=1)
        pockets_btn.callback = self._on_saved_pockets
        self.add_item(pockets_btn)

        last_drop = _get_user_last_drop(self.user_id)
        if last_drop:
            redrop_btn = discord.ui.Button(label="Re-drop Last Cart", emoji="🔁", style=discord.ButtonStyle.success, row=1)
            redrop_btn.callback = self._on_redrop_last
            self.add_item(redrop_btn)

        # Row 2: Browse all, Custom, Close
        browse_btn = discord.ui.Button(label="Browse All Items", emoji="📦", style=discord.ButtonStyle.secondary, row=2)
        browse_btn.callback = self._on_browse_all
        self.add_item(browse_btn)

        custom_btn = discord.ui.Button(label="Custom / Hex", emoji="📝", style=discord.ButtonStyle.secondary, row=2)
        custom_btn.callback = self._on_custom_modal
        self.add_item(custom_btn)

        close_btn = discord.ui.Button(label="Close", emoji="✖", style=discord.ButtonStyle.secondary, row=2)
        close_btn.callback = self._on_close
        self.add_item(close_btn)

    async def _on_saved_pockets(self, interaction: discord.Interaction) -> None:
        view = SavedPocketPickerView(self.island_name, self.user_id, parent_view=self)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    async def _on_redrop_last(self, interaction: discord.Interaction) -> None:
        last = _get_user_last_drop(self.user_id)
        if not last:
            await interaction.response.send_message("No previous drop found.", ephemeral=True)
            return

        confirm_embed = discord.Embed(
            title="🔁 Confirm Re-Drop Previous Cart",
            description=(
                f"You are about to re-drop your previous order on **[{self.island_name}]({_island_url(self.island_name)})**.\n\n"
                f"**Items:**\n```{last['display_name'][:300]}```\n"
                "👉 **Make sure you are standing on the island before confirming!**\n"
                "Items will drop near your character in a few seconds."
            ),
            color=PANEL_EMBED_COLOR,
        )
        confirm_embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
        confirm_view = PresetDropConfirmView(
            self.island_name, self.user_id, last["payload"], f"Last Order ({last['display_name'][:30]})", emoji="🔁"
        )
        await interaction.response.edit_message(embed=confirm_embed, view=_adopt(confirm_view, interaction))

    async def _on_preset_select(self, interaction: discord.Interaction) -> None:
        vals = (interaction.data or {}).get("values", [])
        select = vals[0] if vals else ""
        if not select:
            return

        catalog = ACNHCatalog.get_instance()
        presets_data = catalog.get_drop_presets() or []
        label = select
        emoji = "📦"
        for l, d, code, em in presets_data:
            if code == select:
                label = l
                emoji = em
                break

        confirm_embed = discord.Embed(
            title=f"📦 Confirm Preset Drop — {label}",
            description=(
                f"You are about to drop **{emoji} {label}** on **[{self.island_name}]({_island_url(self.island_name)})**.\n\n"
                "👉 **Make sure you are standing on the island before confirming!**\n"
                "Items will drop near your character in a few seconds."
            ),
            color=PANEL_EMBED_COLOR,
        )
        confirm_embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
        confirm_view = PresetDropConfirmView(self.island_name, self.user_id, select, label, emoji)
        await interaction.response.edit_message(embed=confirm_embed, view=_adopt(confirm_view, interaction))

    async def _on_search_catalog(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(CatalogSearchModal(island_name=self.island_name))

    async def _on_browse_all(self, interaction: discord.Interaction) -> None:
        await _show_catalog(interaction, self.island_name, query="")

    async def _on_custom_modal(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(CustomDropModal(island_name=self.island_name))

    async def _on_close(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(content="Menu closed.", embed=None, view=None)
        self.stop()
