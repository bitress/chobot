"""
Chorder - Standalone SysBot Order Bot & Interactive Order Panel
Author: ChoPaeng
Features:
- /orderpanel: Deployable interactive Order Station in any channel.
- Add to Cart & Cart Builder with 40-pocket slot limit.
- 16-Character Hex & Variant Encoding for SysBot order accuracy.
- My Orders: Synced with website database (order_bot_queue), live ETA, Dodo code, cancel button.
- Live Queues: Real-time SysBot queue viewer with refresh.
- Presets: Live bundles from https://console.chopaeng.com/api/bundles.
- Search Catalog: Interactive GUI with item/villager images and multi-select support.
"""

import asyncio
import logging
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands

# Add parent directory to path if run standalone
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils.config import Config
from utils.acnh_catalog import ACNHCatalog, CatalogItem, CatalogVillager, generate_full_item_hex
from utils.sysbot_api import SysBotClient, parse_order_input, _INVALID_DODO_CODES

# ============================================================================
# LOGGING SETUP
# ============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
)
logger = logging.getLogger("Chorder")

# ============================================================================
# CART DATA MODEL
# ============================================================================
MAX_POCKET_SLOTS = 40


class CartItem:
    def __init__(
        self,
        name: str,
        quantity: int = 1,
        category: str = "Items",
        image_url: str = "",
        variation: str = "",
        variant_id: str = "",
        internal_id: str = "",
        diy: bool = False,
    ):
        self.name = name
        self.quantity = max(1, quantity)
        self.category = category
        self.image_url = image_url
        self.variation = variation
        self.variant_id = variant_id
        self.internal_id = internal_id
        self.diy = diy

    @property
    def display_name(self) -> str:
        if self.variation and self.variation.lower() not in ("na", "none", ""):
            return f"{self.name} ({self.variation})"
        return self.name

    def to_hex_code(self) -> str:
        """Get the full 16-character / 4-character item hex code."""
        if self.internal_id:
            return generate_full_item_hex(
                base_id=self.internal_id,
                variant_string=self.variant_id or self.variation,
                category=self.category,
            )
        return ""

    def to_command_string(self) -> str:
        """
        Generate order string for this item.
        If hex code is available, repeats hex code for quantity.
        Otherwise falls back to display name.
        """
        hex_code = self.to_hex_code()
        if hex_code:
            if self.quantity > 1:
                return " ".join([hex_code] * self.quantity)
            return hex_code
        if self.quantity > 1:
            return f"{self.display_name} {self.quantity}"
        return self.display_name


class UserCart:
    """In-memory cart for an interactive user session."""
    def __init__(self, user_id: int):
        self.user_id = user_id
        self.items: List[CartItem] = []
        self.villager: Optional[CatalogVillager] = None
        self.last_activity: float = time.time()

    def get_pocket_count(self) -> int:
        return sum(item.quantity for item in self.items)

    def is_full(self) -> bool:
        return self.get_pocket_count() >= MAX_POCKET_SLOTS

    def add_item(
        self,
        name: str,
        quantity: int = 1,
        category: str = "Items",
        image_url: str = "",
        variation: str = "",
        variant_id: str = "",
        internal_id: str = "",
        diy: bool = False,
    ) -> bool:
        self.last_activity = time.time()
        current_count = self.get_pocket_count()
        if current_count >= MAX_POCKET_SLOTS:
            return False

        qty_to_add = min(quantity, MAX_POCKET_SLOTS - current_count)
        if qty_to_add <= 0:
            return False

        # If duplicate item exists, update quantity
        for existing in self.items:
            if (
                existing.name.lower() == name.lower()
                and existing.variation.lower() == variation.lower()
                and existing.internal_id == internal_id
            ):
                existing.quantity += qty_to_add
                return True

        self.items.append(
            CartItem(
                name=name,
                quantity=qty_to_add,
                category=category,
                image_url=image_url,
                variation=variation,
                variant_id=variant_id,
                internal_id=internal_id,
                diy=diy,
            )
        )
        return True

    def set_villager(self, villager: CatalogVillager) -> None:
        self.last_activity = time.time()
        self.villager = villager

    def remove_villager(self) -> None:
        self.last_activity = time.time()
        self.villager = None

    def remove_item(self, index: int) -> bool:
        self.last_activity = time.time()
        if 0 <= index < len(self.items):
            self.items.pop(index)
            return True
        return False

    def clear(self) -> None:
        self.last_activity = time.time()
        self.items.clear()
        self.villager = None

    def to_order_string(self) -> str:
        """Build SysBot order command string."""
        item_tokens = []
        for item in self.items:
            tok = item.to_command_string()
            if tok:
                item_tokens.append(tok)

        villager_part = f"villager:{self.villager.villager_id or self.villager.name}" if self.villager else None

        # Check if all tokens are pure hex codes and spaces
        all_hex = all(
            bool(re.match(r"^[0-9A-Fa-f\s]+$", tok)) for tok in item_tokens
        ) if item_tokens else False

        if all_hex:
            items_str = " ".join(item_tokens)
            if villager_part:
                return f"{items_str} {villager_part}".strip()
            return items_str
        else:
            if villager_part:
                item_tokens.append(villager_part)
            return ", ".join(item_tokens)


class CartManager:
    """Manages active user carts."""
    def __init__(self):
        self._carts: Dict[int, UserCart] = {}

    def get_cart(self, user_id: int) -> UserCart:
        if user_id not in self._carts:
            self._carts[user_id] = UserCart(user_id)
        return self._carts[user_id]


cart_manager = CartManager()
catalog = ACNHCatalog.get_instance()
sysbot = SysBotClient()

# ============================================================================
# EMBED FACTORIES & UI HELPERS
# ============================================================================
EMBED_COLOR_DEFAULT = 0x2ECC71  # ACNH Green
EMBED_COLOR_ORDER = 0x3498DB    # Blue
EMBED_COLOR_WARN = 0xE67E22     # Orange
EMBED_COLOR_ERROR = 0xE74C3C    # Red


def build_panel_embed(island_name: str = "Sinta", is_online: bool = True) -> discord.Embed:
    """Build the main Order Station dashboard embed."""
    status_emoji = "🟢" if is_online else "🔴"
    status_text = "Online & Accepting Orders" if is_online else "Offline / Maintenance"

    embed = discord.Embed(
        title="🍃 ChoPaeng ACNH Order Station",
        description=(
            f"Welcome to the **SysBot Order Terminal**! Build custom inventory pockets, "
            f"order dream villagers, and enjoy automated fast delivery directly to your island.\n\n"
            f"**Bot Status:** {status_emoji} `{status_text}`\n"
            f"**Active Island:** 🏝️ `{island_name}`\n"
            f"**Pocket Capacity:** 🎒 `Max 40 Slots` + `1 Villager Plot`\n"
        ),
        color=EMBED_COLOR_DEFAULT if is_online else EMBED_COLOR_WARN,
    )
    embed.add_field(
        name="🛒 Quick Guide",
        value=(
            "• **Add to Cart / View Cart:** Manage your 40 inventory pocket slots.\n"
            "• **Search Catalog:** Search items & villagers with multi-select and images.\n"
            "• **Presets / Bundles:** Load pre-made item bundles in 1 click.\n"
            "• **My Orders:** View real-time queue position, ETA, and your Dodo Code.\n"
            "• **Live Queue:** Check current SysBot island activity.\n"
            "• **Direct Command:** `!order <item> [villager:id]`"
        ),
        inline=False,
    )
    embed.set_thumbnail(url="https://nh-cdn.catalogue.ac/NpcIcon/brd09.png")
    embed.set_footer(text="ChoPaeng Orders • Synced with chopaeng.com", icon_url="https://nh-cdn.catalogue.ac/NpcIcon/cat23.png")
    return embed


def build_cart_embed(cart: UserCart, user: discord.User | discord.Member) -> discord.Embed:
    """Build the interactive Cart viewer embed."""
    slots_used = cart.get_pocket_count()
    slots_left = MAX_POCKET_SLOTS - slots_used

    # Visual pocket bar
    bar_filled = int((slots_used / MAX_POCKET_SLOTS) * 12)
    bar_empty = 12 - bar_filled
    bar_str = "█" * bar_filled + "░" * bar_empty

    embed = discord.Embed(
        title=f"🎒 {user.display_name}'s Cart & Pocket Builder",
        description=(
            f"**Pocket Capacity:** `[{bar_str}]` **{slots_used} / {MAX_POCKET_SLOTS} slots** "
            f"({slots_left} remaining)\n"
        ),
        color=EMBED_COLOR_ORDER,
    )

    if cart.villager:
        embed.add_field(
            name="🏡 Selected Villager (Move-in Plot)",
            value=f"**{cart.villager.name}** (`ID: {cart.villager.villager_id}`) • *{cart.villager.species} / {cart.villager.personality}*",
            inline=False,
        )
        if cart.villager.photo_url:
            embed.set_thumbnail(url=cart.villager.photo_url)
    elif cart.items and cart.items[0].image_url:
        embed.set_thumbnail(url=cart.items[0].image_url)

    if not cart.items and not cart.villager:
        embed.add_field(
            name="Cart is Empty",
            value="Your cart is currently empty! Use **Search Catalog** or **Add Item** below to begin.",
            inline=False,
        )
    else:
        # Group items into lines
        lines = []
        for i, item in enumerate(cart.items, 1):
            qty_str = f" x{item.quantity}" if item.quantity > 1 else ""
            cat_badge = f"[{item.category}]"
            lines.append(f"`{i:02d}.` **{item.display_name}**{qty_str} *{cat_badge}*")

        # Split into chunks if too long
        chunk_text = "\n".join(lines[:20])
        embed.add_field(name="📦 Pocket Items", value=chunk_text, inline=False)
        if len(lines) > 20:
            more_text = "\n".join(lines[20:40])
            embed.add_field(name="📦 Pocket Items (Continued)", value=more_text, inline=False)

    order_cmd = cart.to_order_string()
    if order_cmd:
        embed.add_field(
            name="⚡ SysBot Command String",
            value=f"```!order {order_cmd[:900]}```",
            inline=False,
        )

    embed.set_footer(text="Click 'Submit Order' when your pockets are ready to fly!")
    return embed


# ============================================================================
# DISCORD UI MODALS & VIEWS
# ============================================================================


class QuickOrderModal(discord.ui.Modal, title="⚡ Quick SysBot Order"):
    order_input = discord.ui.TextInput(
        label="Items & Villager",
        style=discord.TextStyle.paragraph,
        placeholder="e.g. Gold nugget 30, Iron nugget 30, villager:Raymond",
        required=True,
        max_length=1000,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        raw_text = self.order_input.value.strip()
        if not raw_text:
            await interaction.followup.send("❌ Please enter valid items or a villager to order.", ephemeral=True)
            return

        username = interaction.user.display_name
        user_id = str(interaction.user.id)

        result = await sysbot.submit_order(
            username=username,
            order_text=raw_text,
            user_id=user_id,
        )

        if result.get("success") or result.get("order_id"):
            order_id = result.get("order_id", "Unknown")
            pos = result.get("queue_position", 1)
            eta = result.get("estimated_minutes", 2)
            island = result.get("island_name", "Sinta")

            embed = discord.Embed(
                title="✅ Order Submitted to SysBot!",
                description=(
                    f"Your order has been placed into the SysBot queue.\n\n"
                    f"**Order ID:** `{order_id}`\n"
                    f"**Queue Position:** `#{pos}`\n"
                    f"**Estimated Time:** `~{eta} min`\n"
                    f"**Island:** 🏝️ `{island}`\n\n"
                    f"**Order Payload:**\n```{raw_text}```\n"
                    f"You will receive your **Dodo Code** when the bot is ready. "
                    f"Track this anytime via the **My Orders** button!"
                ),
                color=EMBED_COLOR_DEFAULT,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            err_msg = result.get("error") or "SysBot could not process this order."
            await interaction.followup.send(f"❌ **Failed to submit order:** {err_msg}", ephemeral=True)


class AddItemModal(discord.ui.Modal, title="➕ Add Item to Cart"):
    item_name = discord.ui.TextInput(
        label="Item Name",
        placeholder="e.g. Gold nugget, Royal crown, Pisces lamp",
        required=True,
        max_length=100,
    )
    quantity = discord.ui.TextInput(
        label="Quantity / Count (Max 30)",
        placeholder="1",
        default="1",
        required=False,
        max_length=3,
    )

    def __init__(self, cart: UserCart):
        super().__init__()
        self.cart = cart

    async def on_submit(self, interaction: discord.Interaction):
        name_str = self.item_name.value.strip()
        qty_str = self.quantity.value.strip() or "1"
        try:
            qty = max(1, min(30, int(qty_str)))
        except ValueError:
            qty = 1

        # Check catalog for image/details
        item_data = catalog.get_item(name_str)
        if item_data:
            added = self.cart.add_item(
                name=item_data.name,
                quantity=qty,
                category=item_data.category,
                image_url=item_data.image_url,
                variation=item_data.variation,
                internal_id=item_data.internal_id,
                diy=item_data.diy,
            )
        else:
            added = self.cart.add_item(name=name_str, quantity=qty)

        if not added:
            await interaction.response.send_message(
                "❌ Cart is full! Maximum 40 pocket slots reached.", ephemeral=True
            )
            return

        embed = build_cart_embed(self.cart, interaction.user)
        view = CartView(self.cart)
        await interaction.response.edit_message(embed=embed, view=view)


class SetVillagerModal(discord.ui.Modal, title="🏡 Set Villager for Move-in Plot"):
    villager_query = discord.ui.TextInput(
        label="Villager Name or ID",
        placeholder="e.g. Raymond, Marshal, brd09, cat23",
        required=True,
        max_length=50,
    )

    def __init__(self, cart: UserCart):
        super().__init__()
        self.cart = cart

    async def on_submit(self, interaction: discord.Interaction):
        query = self.villager_query.value.strip()
        villager = catalog.get_villager(query)

        if not villager:
            # Fallback custom villager
            villager = CatalogVillager(name=query, villager_id=query)

        self.cart.set_villager(villager)
        embed = build_cart_embed(self.cart, interaction.user)
        view = CartView(self.cart)
        await interaction.response.edit_message(embed=embed, view=view)


class CatalogSearchModal(discord.ui.Modal, title="🔍 Search ACNH Catalog"):
    search_query = discord.ui.TextInput(
        label="Search Keyword (Items & Villagers)",
        placeholder="e.g. painting, crown, diy, Raymond, gold",
        required=True,
        max_length=60,
    )

    def __init__(self, cart: UserCart):
        super().__init__()
        self.cart = cart

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        query = self.search_query.value.strip()
        items = catalog.search_items(query, limit=15)
        villagers = catalog.search_villagers(query, limit=5)

        view = CatalogSearchView(self.cart, query=query, items=items, villagers=villagers)
        embed = view.build_search_embed()
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


# ── Catalog Search View (with Multi-Select & Images) ──────────────────────────


class CatalogSearchView(discord.ui.View):
    """Catalog search results view with interactive multi-select and images."""

    def __init__(
        self,
        cart: UserCart,
        query: str = "",
        category: Optional[str] = None,
        items: Optional[List[CatalogItem]] = None,
        villagers: Optional[List[CatalogVillager]] = None,
    ):
        super().__init__(timeout=300)
        self.cart = cart
        self.query = query
        self.category = category
        self.items = items if items is not None else catalog.search_items(query, limit=15, category=category)
        self.villagers = villagers if villagers is not None else catalog.search_villagers(query, limit=5)

        self._build_components()

    def _build_components(self):
        self.clear_items()

        # Multi-select dropdown for items (if available)
        if self.items:
            options = []
            for idx, it in enumerate(self.items[:15]):
                desc = f"[{it.category}]"
                if it.variation:
                    desc += f" Var: {it.variation}"
                options.append(
                    discord.SelectOption(
                        label=it.display_name[:100],
                        description=desc[:100],
                        value=f"item_{idx}",
                        emoji="📦" if not it.diy else "📜",
                    )
                )

            max_opts = min(len(options), 10)
            item_select = discord.ui.Select(
                placeholder=f"Select items to add to cart (multi-select up to {max_opts})...",
                min_values=1,
                max_values=max_opts,
                options=options,
                custom_id="catalog_multi_select",
                row=0,
            )
            item_select.callback = self.on_item_select
            self.add_item(item_select)

        # Dropdown for villagers (if available)
        if self.villagers:
            v_options = []
            for idx, v in enumerate(self.villagers[:10]):
                v_options.append(
                    discord.SelectOption(
                        label=v.display_name[:100],
                        description=f"ID: {v.villager_id} • {v.personality}"[:100],
                        value=f"villager_{idx}",
                        emoji="🐱",
                    )
                )

            villager_select = discord.ui.Select(
                placeholder="Select a villager for your move-in plot...",
                min_values=1,
                max_values=1,
                options=v_options,
                custom_id="villager_select",
                row=1,
            )
            villager_select.callback = self.on_villager_select
            self.add_item(villager_select)

        # Action buttons
        btn_search = discord.ui.Button(label="New Search", style=discord.ButtonStyle.primary, emoji="🔍", row=2)
        btn_search.callback = self.on_search_button
        self.add_item(btn_search)

        btn_cart = discord.ui.Button(
            label=f"View Cart ({self.cart.get_pocket_count()}/40)",
            style=discord.ButtonStyle.secondary,
            emoji="🛒",
            row=2,
        )
        btn_cart.callback = self.on_view_cart
        self.add_item(btn_cart)

    def build_search_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=f"🔍 Catalog Search: '{self.query or 'All'}'",
            description=(
                f"Found **{len(self.items)}** items and **{len(self.villagers)}** villagers.\n"
                f"Use the **dropdowns below** to multi-select items into your cart!\n"
            ),
            color=EMBED_COLOR_DEFAULT,
        )

        # Show thumbnail from top match
        if self.villagers and self.villagers[0].photo_url:
            embed.set_thumbnail(url=self.villagers[0].photo_url)
        elif self.items and self.items[0].image_url:
            embed.set_thumbnail(url=self.items[0].image_url)

        # Items listing
        if self.items:
            lines = []
            for i, it in enumerate(self.items[:10], 1):
                diy_badge = " *(DIY)*" if it.diy else ""
                lines.append(f"`{i}.` **{it.display_name}**{diy_badge} — `{it.category}`")
            embed.add_field(name="📦 Matching Items", value="\n".join(lines), inline=False)

        # Villagers listing
        if self.villagers:
            v_lines = []
            for v in self.villagers[:5]:
                v_lines.append(f"• **{v.name}** (`ID: {v.villager_id}`) — *{v.species} / {v.personality}*")
            embed.add_field(name="🏡 Matching Villagers", value="\n".join(v_lines), inline=False)

        if not self.items and not self.villagers:
            embed.description = "❌ No items or villagers found matching that query. Try another keyword!"

        embed.set_footer(text="Multi-select items to add them in a single click!")
        return embed

    async def on_item_select(self, interaction: discord.Interaction):
        selected_values = interaction.data.get("values", [])
        added_names = []
        for val in selected_values:
            idx = int(val.replace("item_", ""))
            if idx < len(self.items):
                it = self.items[idx]
                if self.cart.add_item(
                    name=it.name,
                    quantity=1,
                    category=it.category,
                    image_url=it.image_url,
                    variation=it.variation,
                    internal_id=it.internal_id,
                    diy=it.diy,
                ):
                    added_names.append(it.display_name)

        self._build_components()
        embed = self.build_search_embed()
        embed.set_footer(text=f"✅ Added {len(added_names)} item(s) to your cart! Total: {self.cart.get_pocket_count()}/40")
        await interaction.response.edit_message(embed=embed, view=self)

    async def on_villager_select(self, interaction: discord.Interaction):
        selected = interaction.data.get("values", [""])[0]
        idx = int(selected.replace("villager_", ""))
        if idx < len(self.villagers):
            v = self.villagers[idx]
            self.cart.set_villager(v)

            self._build_components()
            embed = self.build_search_embed()
            embed.set_footer(text=f"✅ Selected villager {v.name} for move-in plot!")
            await interaction.response.edit_message(embed=embed, view=self)

    async def on_search_button(self, interaction: discord.Interaction):
        await interaction.response.send_modal(CatalogSearchModal(self.cart))

    async def on_view_cart(self, interaction: discord.Interaction):
        embed = build_cart_embed(self.cart, interaction.user)
        view = CartView(self.cart)
        await interaction.response.edit_message(embed=embed, view=view)


# ── Presets / Bundles View ───────────────────────────────────────────────────


class BundlesView(discord.ui.View):
    """Presets and bundles browser from https://console.chopaeng.com/api/bundles."""

    def __init__(self, cart: UserCart, bundles: List[Dict[str, Any]]):
        super().__init__(timeout=300)
        self.cart = cart
        self.bundles = bundles
        self.selected_bundle: Optional[Dict[str, Any]] = bundles[0] if bundles else None
        self._build_components()

    def _build_components(self):
        self.clear_items()

        if self.bundles:
            options = []
            for idx, b in enumerate(self.bundles[:20]):
                name = b.get("name", f"Bundle {idx+1}")
                category = b.get("category", "General")
                items_cnt = len(b.get("orderItems", []))
                options.append(
                    discord.SelectOption(
                        label=name[:100],
                        description=f"[{category}] {items_cnt} items"[:100],
                        value=f"bundle_{idx}",
                        emoji="📦",
                    )
                )

            select = discord.ui.Select(
                placeholder="Choose a preset bundle...",
                options=options,
                custom_id="bundle_select",
                row=0,
            )
            select.callback = self.on_bundle_select
            self.add_item(select)

        # Buttons
        btn_load = discord.ui.Button(
            label="Load Bundle to Cart",
            style=discord.ButtonStyle.primary,
            emoji="🛒",
            row=1,
        )
        btn_load.callback = self.on_load_cart
        self.add_item(btn_load)

        btn_order = discord.ui.Button(
            label="Instant Order Bundle",
            style=discord.ButtonStyle.success,
            emoji="🚀",
            row=1,
        )
        btn_order.callback = self.on_instant_order
        self.add_item(btn_order)

        btn_cart = discord.ui.Button(
            label=f"View Cart ({self.cart.get_pocket_count()}/40)",
            style=discord.ButtonStyle.secondary,
            emoji="🎒",
            row=1,
        )
        btn_cart.callback = self.on_view_cart
        self.add_item(btn_cart)

    def build_bundle_embed(self) -> discord.Embed:
        if not self.selected_bundle:
            return discord.Embed(
                title="📦 Preset Bundles",
                description="No preset bundles available at the moment.",
                color=EMBED_COLOR_WARN,
            )

        b = self.selected_bundle
        name = b.get("name", "Preset Bundle")
        desc = b.get("description") or "Official pre-configured ACNH item set."
        category = b.get("category", "Popular")
        items = b.get("orderItems", [])

        embed = discord.Embed(
            title=f"📦 Preset: {name}",
            description=f"**Category:** `{category}`\n{desc}\n\n**Included Items ({len(items)}):**",
            color=EMBED_COLOR_DEFAULT,
        )

        if items:
            # Set thumbnail from first item
            first_img = items[0].get("image")
            if first_img:
                embed.set_thumbnail(url=first_img)

            lines = []
            for i, it in enumerate(items[:20], 1):
                it_name = it.get("name", "Unknown Item")
                qty = it.get("quantity", 1)
                qty_str = f" x{qty}" if qty > 1 else ""
                lines.append(f"`{i:02d}.` **{it_name}**{qty_str}")

            embed.add_field(name="Item Breakdown", value="\n".join(lines), inline=False)
            if len(items) > 20:
                more_lines = [f"`{i:02d}.` **{it.get('name')}**" for i, it in enumerate(items[20:40], 21)]
                embed.add_field(name="Item Breakdown (Cont.)", value="\n".join(more_lines), inline=False)

        embed.set_footer(text="Click 'Load Bundle to Cart' or 'Instant Order Bundle'")
        return embed

    async def on_bundle_select(self, interaction: discord.Interaction):
        val = interaction.data.get("values", [""])[0]
        idx = int(val.replace("bundle_", ""))
        if idx < len(self.bundles):
            self.selected_bundle = self.bundles[idx]
            embed = self.build_bundle_embed()
            await interaction.response.edit_message(embed=embed, view=self)

    async def on_load_cart(self, interaction: discord.Interaction):
        if not self.selected_bundle:
            await interaction.response.send_message("❌ No bundle selected.", ephemeral=True)
            return

        items = self.selected_bundle.get("orderItems", [])
        added_count = 0
        for it in items:
            name = it.get("name")
            qty = it.get("quantity", 1)
            cat = it.get("category", "Items")
            img = it.get("image", "")
            if name and self.cart.add_item(name=name, quantity=qty, category=cat, image_url=img):
                added_count += 1

        self._build_components()
        embed = self.build_bundle_embed()
        embed.set_footer(text=f"✅ Loaded {added_count} items into your cart! Total: {self.cart.get_pocket_count()}/40")
        await interaction.response.edit_message(embed=embed, view=self)

    async def on_instant_order(self, interaction: discord.Interaction):
        if not self.selected_bundle:
            await interaction.response.send_message("❌ No bundle selected.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        items = self.selected_bundle.get("orderItems", [])
        order_parts = []
        for it in items:
            name = it.get("name")
            qty = it.get("quantity", 1)
            if name:
                order_parts.append(f"{name} {qty}" if qty > 1 else name)

        raw_cmd = ", ".join(order_parts)
        result = await sysbot.submit_order(
            username=interaction.user.display_name,
            order_text=raw_cmd,
            user_id=str(interaction.user.id),
        )

        if result.get("success") or result.get("order_id"):
            order_id = result.get("order_id", "Unknown")
            pos = result.get("queue_position", 1)
            eta = result.get("estimated_minutes", 2)
            embed = discord.Embed(
                title="✅ Bundle Ordered!",
                description=(
                    f"**Bundle:** `{self.selected_bundle.get('name')}`\n"
                    f"**Order ID:** `{order_id}`\n"
                    f"**Queue Position:** `#{pos}`\n"
                    f"**Estimated Time:** `~{eta} min`\n\n"
                    f"Track this order anytime under **My Orders**."
                ),
                color=EMBED_COLOR_DEFAULT,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.followup.send(f"❌ Failed to order bundle: {result.get('error')}", ephemeral=True)

    async def on_view_cart(self, interaction: discord.Interaction):
        embed = build_cart_embed(self.cart, interaction.user)
        view = CartView(self.cart)
        await interaction.response.edit_message(embed=embed, view=view)


# ── Cart View ────────────────────────────────────────────────────────────────


class CartView(discord.ui.View):
    """Interactive Cart Management View."""

    def __init__(self, cart: UserCart):
        super().__init__(timeout=300)
        self.cart = cart
        self._build_components()

    def _build_components(self):
        self.clear_items()

        # If items exist, add remove select menu
        if self.cart.items:
            options = []
            for i, it in enumerate(self.cart.items[:25]):
                options.append(
                    discord.SelectOption(
                        label=f"{i+1}. {it.display_name}"[:100],
                        description=f"Qty: {it.quantity} • {it.category}"[:100],
                        value=f"rem_{i}",
                        emoji="🗑️",
                    )
                )
            rem_select = discord.ui.Select(
                placeholder="Remove an item from cart...",
                options=options,
                custom_id="cart_remove_select",
                row=0,
            )
            rem_select.callback = self.on_remove_item
            self.add_item(rem_select)

        # Row 1 Buttons
        btn_submit = discord.ui.Button(
            label="Submit Order to SysBot",
            style=discord.ButtonStyle.success,
            emoji="🚀",
            row=1,
        )
        btn_submit.callback = self.on_submit_order
        self.add_item(btn_submit)

        btn_add = discord.ui.Button(
            label="Add Item",
            style=discord.ButtonStyle.primary,
            emoji="➕",
            row=1,
        )
        btn_add.callback = self.on_add_item
        self.add_item(btn_add)

        btn_villager = discord.ui.Button(
            label="Set Villager",
            style=discord.ButtonStyle.secondary,
            emoji="🐱",
            row=1,
        )
        btn_villager.callback = self.on_set_villager
        self.add_item(btn_villager)

        # Row 2 Buttons
        btn_search = discord.ui.Button(
            label="Browse Catalog",
            style=discord.ButtonStyle.secondary,
            emoji="🔍",
            row=2,
        )
        btn_search.callback = self.on_browse_catalog
        self.add_item(btn_search)

        btn_clear = discord.ui.Button(
            label="Clear Cart",
            style=discord.ButtonStyle.danger,
            emoji="🧹",
            row=2,
        )
        btn_clear.callback = self.on_clear_cart
        self.add_item(btn_clear)

    async def on_submit_order(self, interaction: discord.Interaction):
        if not self.cart.items and not self.cart.villager:
            await interaction.response.send_message("❌ Cart is empty! Add items or a villager before submitting.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        order_cmd = self.cart.to_order_string()
        username = interaction.user.display_name
        user_id = str(interaction.user.id)

        result = await sysbot.submit_order(
            username=username,
            order_text=order_cmd,
            user_id=user_id,
        )

        if result.get("success") or result.get("order_id"):
            order_id = result.get("order_id", "Unknown")
            pos = result.get("queue_position", 1)
            eta = result.get("estimated_minutes", 2)
            island = result.get("island_name", "Sinta")

            # Clear cart on successful submission
            self.cart.clear()

            embed = discord.Embed(
                title="✅ Order Successfully Placed!",
                description=(
                    f"Your pocket order has been submitted to **SysBot**!\n\n"
                    f"**Order ID:** `{order_id}`\n"
                    f"**Queue Position:** `#{pos}`\n"
                    f"**Estimated Arrival:** `~{eta} min`\n"
                    f"**Island:** 🏝️ `{island}`\n\n"
                    f"**Order Breakdown:**\n```{order_cmd[:900]}```\n"
                    f"When your order is ready, your **Dodo Code** will appear under **My Orders**."
                ),
                color=EMBED_COLOR_DEFAULT,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            err = result.get("error") or "Order could not be submitted."
            await interaction.followup.send(f"❌ **Submission Failed:** {err}", ephemeral=True)

    async def on_add_item(self, interaction: discord.Interaction):
        await interaction.response.send_modal(AddItemModal(self.cart))

    async def on_set_villager(self, interaction: discord.Interaction):
        await interaction.response.send_modal(SetVillagerModal(self.cart))

    async def on_remove_item(self, interaction: discord.Interaction):
        val = interaction.data.get("values", [""])[0]
        idx = int(val.replace("rem_", ""))
        self.cart.remove_item(idx)
        self._build_components()
        embed = build_cart_embed(self.cart, interaction.user)
        await interaction.response.edit_message(embed=embed, view=self)

    async def on_clear_cart(self, interaction: discord.Interaction):
        self.cart.clear()
        self._build_components()
        embed = build_cart_embed(self.cart, interaction.user)
        await interaction.response.edit_message(embed=embed, view=self)

    async def on_browse_catalog(self, interaction: discord.Interaction):
        await interaction.response.send_modal(CatalogSearchModal(self.cart))


# ── My Orders View (Synced with Website) ─────────────────────────────────────


class MyOrdersView(discord.ui.View):
    """View and track user orders (synced with website SQLite DB)."""

    def __init__(self, user_id: str, username: str, orders: List[Dict[str, Any]]):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.username = username
        self.orders = orders
        self._build_components()

    def _build_components(self):
        self.clear_items()

        btn_refresh = discord.ui.Button(label="Refresh Status", style=discord.ButtonStyle.primary, emoji="🔄")
        btn_refresh.callback = self.on_refresh
        self.add_item(btn_refresh)

        # If active order exists, allow cancellation
        active_order = self.get_active_order()
        if active_order:
            btn_cancel = discord.ui.Button(
                label=f"Cancel Order ({active_order.get('id', '')[:8]})",
                style=discord.ButtonStyle.danger,
                emoji="❌",
            )
            btn_cancel.callback = self.on_cancel_order
            self.add_item(btn_cancel)

    def get_active_order(self) -> Optional[Dict[str, Any]]:
        for o in self.orders:
            st = str(o.get("status") or "").lower()
            if st in ("queued", "preparing", "ready", "active", "next"):
                return o
        return None

    def build_orders_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=f"📋 My Orders — {self.username}",
            description="Synced with your account and website orders.",
            color=EMBED_COLOR_ORDER,
        )

        active = self.get_active_order()
        if active:
            st = str(active.get("status") or "queued").upper()
            dodo = active.get("dodo_code")
            pos = active.get("queue_position", 1)
            eta = active.get("estimated_minutes", 2)
            island = active.get("island_name", "Sinta")
            cmd = active.get("command", "")

            status_icon = "🟢" if st == "READY" else ("🟡" if st == "PREPARING" else "⏳")

            val = (
                f"**Status:** {status_icon} `{st}`\n"
                f"**Order ID:** `{active.get('id')}`\n"
                f"**Queue Position:** `#{pos}` | **ETA:** `~{eta} min`\n"
                f"**Island:** 🏝️ `{island}`\n"
            )
            if dodo and str(dodo).strip() not in _INVALID_DODO_CODES:
                val += f"\n✈️ **DODO CODE:** ```\n{dodo}\n```\n*Fly to {island} now! Pick up items near airport.*"
            else:
                val += "\n*Dodo code will appear here once ready.*"

            if cmd:
                val += f"\n**Items:** `{cmd[:300]}`"

            embed.add_field(name="⚡ Active Order", value=val, inline=False)
        else:
            embed.add_field(
                name="No Active Orders",
                value="You do not have any pending orders in the queue.",
                inline=False,
            )

        # Past orders history
        past = [o for o in self.orders if o != active][:5]
        if past:
            lines = []
            for o in past:
                st = str(o.get("status") or "").upper()
                ts = o.get("created_at")
                time_str = f"<t:{ts}:R>" if ts else ""
                lines.append(f"• `{o.get('id')[:10]}` — `{st}` {time_str}")
            embed.add_field(name="📜 Recent Order History", value="\n".join(lines), inline=False)

        embed.set_footer(text="Auto-refreshed from SysBot & database")
        return embed

    async def on_refresh(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        # Fetch latest from DB and check active order status on SysBot
        self.orders = await sysbot.get_user_order_history(self.user_id, limit=10)
        active = self.get_active_order()
        if active and active.get("id"):
            fresh = await sysbot.get_order_status(active["id"])
            if fresh.get("status"):
                active.update(fresh)

        self._build_components()
        embed = self.build_orders_embed()
        await interaction.followup.edit_message(message_id=interaction.message.id, embed=embed, view=self)

    async def on_cancel_order(self, interaction: discord.Interaction):
        active = self.get_active_order()
        if not active:
            await interaction.response.send_message("❌ No active order found to cancel.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        res = await sysbot.cancel_order(active["id"])
        self.orders = await sysbot.get_user_order_history(self.user_id, limit=10)
        self._build_components()
        embed = self.build_orders_embed()
        await interaction.followup.send("✅ Order has been cancelled.", ephemeral=True)
        await interaction.message.edit(embed=embed, view=self)


# ── Live Queue View ──────────────────────────────────────────────────────────


class LiveQueueView(discord.ui.View):
    """View current SysBot orders queue."""

    def __init__(self, queue_data: dict, bot_status: dict):
        super().__init__(timeout=180)
        self.queue_data = queue_data
        self.bot_status = bot_status
        self._build_components()

    def _build_components(self):
        self.clear_items()
        btn_refresh = discord.ui.Button(label="Refresh Queue", style=discord.ButtonStyle.primary, emoji="🔄")
        btn_refresh.callback = self.on_refresh
        self.add_item(btn_refresh)

    def build_queue_embed(self) -> discord.Embed:
        island = self.bot_status.get("island_name", "Sinta")
        is_online = self.bot_status.get("is_running", True)
        orders = self.queue_data.get("orders") or self.queue_data.get("queue") or []

        embed = discord.Embed(
            title=f"⏱️ Live Order Queue — {island}",
            description=(
                f"**Island Status:** {'🟢 Online' if is_online else '🔴 Offline'}\n"
                f"**Orders in Queue:** `{len(orders)}`\n"
            ),
            color=EMBED_COLOR_ORDER,
        )

        if orders:
            lines = []
            for idx, o in enumerate(orders[:10], 1):
                user = o.get("username") or o.get("user") or "Anonymous"
                st = str(o.get("status") or "queued").upper()
                eta = o.get("estimated_minutes") or o.get("eta") or 2
                lines.append(f"`#{idx:02d}` **{user}** — `{st}` (~{eta}m)")

            embed.add_field(name="Current Queue", value="\n".join(lines), inline=False)
            if len(orders) > 10:
                embed.add_field(name="Remaining", value=f"*+{len(orders)-10} more in line...*", inline=False)
        else:
            embed.add_field(
                name="Queue Empty",
                value="No orders in queue! Place an order to be served immediately.",
                inline=False,
            )

        embed.set_footer(text=f"Last updated: {time.strftime('%H:%M:%S UTC')}")
        return embed

    async def on_refresh(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        self.queue_data = await sysbot.get_queue()
        self.bot_status = await sysbot.get_bot_status()
        self._build_components()
        embed = self.build_queue_embed()
        await interaction.followup.edit_message(message_id=interaction.message.id, embed=embed, view=self)


# ── Main Order Panel View (Channel Deployable) ───────────────────────────────


class OrderPanelView(discord.ui.View):
    """Persistent interactive panel deployed in channels."""

    def __init__(self):
        super().__init__(timeout=None)  # Persistent view

    @discord.ui.button(
        label="Add to Cart / My Cart",
        style=discord.ButtonStyle.primary,
        emoji="🛒",
        custom_id="panel_btn_cart",
        row=0,
    )
    async def btn_cart(self, interaction: discord.Interaction, button: discord.ui.Button):
        cart = cart_manager.get_cart(interaction.user.id)
        embed = build_cart_embed(cart, interaction.user)
        view = CartView(cart)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(
        label="Search Catalog",
        style=discord.ButtonStyle.secondary,
        emoji="🔍",
        custom_id="panel_btn_search",
        row=0,
    )
    async def btn_search(self, interaction: discord.Interaction, button: discord.ui.Button):
        cart = cart_manager.get_cart(interaction.user.id)
        await interaction.response.send_modal(CatalogSearchModal(cart))

    @discord.ui.button(
        label="Presets / Bundles",
        style=discord.ButtonStyle.secondary,
        emoji="📦",
        custom_id="panel_btn_presets",
        row=0,
    )
    async def btn_presets(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        cart = cart_manager.get_cart(interaction.user.id)
        bundles = await sysbot.get_bundles()
        view = BundlesView(cart, bundles)
        embed = view.build_bundle_embed()
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(
        label="My Orders",
        style=discord.ButtonStyle.secondary,
        emoji="📋",
        custom_id="panel_btn_orders",
        row=1,
    )
    async def btn_orders(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        user_id = str(interaction.user.id)
        orders = await sysbot.get_user_order_history(user_id, limit=10)
        view = MyOrdersView(user_id, interaction.user.display_name, orders)
        embed = view.build_orders_embed()
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(
        label="Live Queue",
        style=discord.ButtonStyle.secondary,
        emoji="⏱️",
        custom_id="panel_btn_queue",
        row=1,
    )
    async def btn_queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        q_data = await sysbot.get_queue()
        status_data = await sysbot.get_bot_status()
        view = LiveQueueView(q_data, status_data)
        embed = view.build_queue_embed()
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(
        label="Quick Order",
        style=discord.ButtonStyle.success,
        emoji="⚡",
        custom_id="panel_btn_quick_order",
        row=1,
    )
    async def btn_quick_order(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(QuickOrderModal())


# ============================================================================
# DISCORD COG & COMMAND HANDLER
# ============================================================================


class ChorderCog(commands.Cog, name="Chorder"):
    """Cog providing the interactive ACNH SysBot Order Panel."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── Slash Command: /orderpanel ──────────────────────────────────────────

    @app_commands.command(
        name="orderpanel",
        description="Deploy the interactive ACNH SysBot Order Panel to a channel",
    )
    @app_commands.describe(channel="The channel to deploy the order panel into (defaults to current)")
    async def slash_orderpanel(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
    ):
        target_channel = channel or interaction.channel
        if not target_channel:
            await interaction.response.send_message("❌ Target channel not found.", ephemeral=True)
            return

        bot_status = await sysbot.get_bot_status()
        island_name = bot_status.get("island_name", getattr(Config, "ORDER_BOT_ISLAND", "Sinta"))
        is_online = bot_status.get("is_running", True)

        embed = build_panel_embed(island_name, is_online)
        view = OrderPanelView()

        await target_channel.send(embed=embed, view=view)
        await interaction.response.send_message(
            f"✅ Order panel deployed to {target_channel.mention}!", ephemeral=True
        )


# ============================================================================
# STANDALONE BOT RUNNER
# ============================================================================


class ChorderBot(commands.Bot):
    """Standalone Discord Bot instance for Chorder."""

    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix="!", intents=intents, help_command=None)

    async def setup_hook(self):
        logger.info("[Chorder] Loading Chorder Cog...")
        await self.add_cog(ChorderCog(self))
        self.add_view(OrderPanelView())  # Register persistent view

        # Sync slash commands
        if Config.GUILD_ID:
            guild_obj = discord.Object(id=Config.GUILD_ID)
            self.tree.copy_global_to(guild=guild_obj)
            await self.tree.sync(guild=guild_obj)
            logger.info(f"[Chorder] Slash commands synced to Guild: {Config.GUILD_ID}")
        else:
            await self.tree.sync()
            logger.info("[Chorder] Slash commands synced globally.")

    async def on_ready(self):
        logger.info(f"[Chorder] Logged in as: {self.user} (ID: {self.user.id})")
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="ACNH Orders | /orderpanel 📦",
            )
        )


def run_standalone():
    """Entry point for standalone execution."""
    token = Config.DISCORD_TOKEN or os.getenv("DISCORD_TOKEN")
    if not token:
        logger.error("[Chorder] DISCORD_TOKEN is not set in .env! Cannot start bot.")
        sys.exit(1)

    logger.info("[Chorder] Starting standalone Chorder Bot...")
    bot = ChorderBot()
    try:
        bot.run(token)
    except KeyboardInterrupt:
        logger.info("[Chorder] Bot stopped by user.")
    except Exception as exc:
        logger.error(f"[Chorder] Bot encountered a fatal error: {exc}", exc_info=True)


if __name__ == "__main__":
    run_standalone()
