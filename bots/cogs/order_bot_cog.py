"""
OrderBot Cog — the chopaeng.com/order experience inside Discord.

Free members build a 40-slot "pocket", send it to the Order Bot island (Config.ORDER_BOT_ISLAND)
and get a private Dodo code when their turn comes up. Orders go through the same SysBot REST API
and the same ``order_bot_queue`` table as the website, so an order placed here shows up in the
website's history and the other way round.

Persistent panel (posted with /orderpanel, refreshed every 30s):
  • 🛒 Start Order   – opens the pocket builder (or the live tracker if an order is already active)
  • 📦 My Order      – live tracker for the active order: queue position, ETA, Dodo, cancel
  • 📋 Queue         – who is in line right now
  • 🕘 History       – the last 10 orders (in item names, also for hex orders) with one-click reorder
  • 📖 Guide         – how ordering works
  • 🌐 Web Order Bot – link to the website
  • 🔔 Notify me     – only while the bot is closed: one DM when it takes orders again
The panel is sticky: when messages bury it, it is reposted at the bottom of the channel. Its status
says how busy the line is (quiet / steady / busy).

Pocket builder (ephemeral). The order screen shows only Find items · Bundles · Place order, the item
list, and Type a list · Villager · More; saved loadouts, custom hex and Clear are under More:
  • Add items by typing (`Gold nugget 30, Royal crown, 09C9 x10`), or search the catalog
    and pick a color/pattern for items that have several
  • Browse: popular items and popular series; results are paged and filterable by category
  • Filters: category, colour, season/event, and the language item names are shown in
    (search and typed items also understand names in the other game languages)
  • The pocket shows a slot bar, groups bigger pockets by category, the newest item's picture, and
    whether the Order Bot is open and how long the line is
  • Tap an item in the pocket to change its quantity (− / + / ×1 / Max / Amount…) or remove it; the
    item menu pages (◀ ▶ options) so all 40 slots are reachable. Remove, Clear and bulk removals offer Undo
  • Place order opens a review screen (full pocket, line length, reminders); Send order submits it
  • Find items opens with the member's recently ordered items first; results show what's already
    in the pocket (🛒×N) and what was ordered before (🕘)
  • Results show a numbered list with a thumbnail; pick one item for its detail screen (picture,
    quantity, colour, custom hex) or several to add them at once; toggle Items / DIYs
  • Edit quantities: every quantity in one text box
  • Custom hex: paste a full `!order` command from the website or raw item hex codes
  • One move-in villager per order
  • Official bundles, personal saved loadouts
  • The pocket is saved per user, so an expired menu or a restart doesn't lose it

Order tracking:
  • The tracker message updates itself while the order moves queued → preparing → ready, with
    "N people ahead of you", a progress bar and who is being served
  • Once the gate opens it counts down to when it closes (Config.ORDER_GATE_SECONDS after the Dodo)
  • An "almost your turn" DM goes out when a member who waited reaches the front of the line
  • The Dodo code is sent by DM the moment the gate opens (and shown in the private tracker)
  • An order that ends without a pickup (missed gate, cancelled by the bot) offers a one-tap reorder
  • Tracking of Discord-placed orders resumes after a bot restart

Slash commands:
  • /order [items] [villager]  – open the pocket, optionally pre-filled
  • /cart                      – open the pocket
  • /myorders                  – active order + history
  • /queue                     – live queue
  • /presets                   – official bundles
  • /orderpanel [remove]       – (Manage Messages) post or move the panel to this channel, or remove it.
                                 A deleted panel is restored automatically, so use `remove` to take it down.

Same gates as the website: moderation blocklist, accepted Order Bot rules, and a
"Character | Island" nickname or a registered in-game character. One active order per member.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils import catalog_filters as cf
from utils.acnh_catalog import ACNHCatalog, CatalogItem
from utils.catalog_filters import CatalogFilters
from utils.config import Config
from utils.database import connect_db
from utils.nickname_format import is_valid_acnh_nickname
from utils.sysbot_api import SysBotClient, get_island_sysbot_client, parse_order_input

from .dropbot.constants import (
    DODO_GIF,
    PANEL_EMBED_COLOR,
    PANEL_EMBED_COLOR_ERR,
    PANEL_EMBED_COLOR_WARN,
    PANEL_FOOTER_ICON,
    PANEL_FOOTER_TEXT,
    STICKY_DELAY_SECONDS,
    STICKY_MAX_WAIT_SECONDS,
    WEBSITE_BASE,
)
from .dropbot.cart import _Variant, _item_variants
from .dropbot.islands import _denied_reason, _has_order_role
from .dropbot.state import _cooldown_left, _pop_cooldown, _spawn
from .dropbot.status import _err_embed, _lookup_saved_character
from .dropbot.ui_base import _OwnedView, _adopt, _reply
from .dropbot.views_filters import CatalogFilterView

logger = logging.getLogger("OrderBotCog")


# ─── Constants ────────────────────────────────────────────────────────────────

ORDER_MAX_SLOTS = 40          # one full pocket, same as the website
SUBMIT_COOLDOWN = 15.0        # seconds between order submissions per member
POLL_SECONDS = 3.0            # tracker poll interval while waiting in line
POLL_SECONDS_READY = 5.0     # ...and once the gate is open
TRACK_MAX_SECONDS = 60 * 60   # stop following an order after an hour
TOKEN_SECONDS = 14 * 60       # an interaction's message can be edited for 15 minutes
ACTIVE_ORDER_WINDOW = 2 * 60 * 60   # ignore "active" DB rows older than this (stale after a crash)
MAX_LOADOUTS = 10
PANEL_REFRESH_SECONDS = 5
RESULTS_PER_PAGE = 25         # catalog results shown per page (Discord's select limit)
SEARCH_LIMIT = 100             # most catalog results kept for one search
CATEGORY_BROWSE_LIMIT = 300    # most items loaded when browsing a whole category
MULTI_PICK_MAX = 10            # most results that can be added in one pick
_KIND_LABELS = {"all": ("Type: All", "🏷️"), "items": ("Type: Items", "📦"), "diy": ("Type: DIYs", "📜")}
NEXT_UP_POSITION = 2          # DM "almost your turn" when a member reaches this place in line
NOTIFY_MAX = 300              # most "open again" DMs sent in one go
PANEL_TITLE_PREFIX = "📦 Order Bot"
POCKET_PAGE_SIZE = 22         # pocket items per page of the item menu (+ ◀ ▶ and the villager = 25 options)
SLOTS_WARN_AT = 5             # "N slots left" once this few remain
GROUP_MIN_LINES = 6           # group the pocket list by category from this many items
RECENT_ITEMS = 5              # past items offered first when Find items opens
STATUS_LINE_MAX_AGE = 60      # seconds a cached "Order Bot is open · N in line" line stays fresh
QUEUE_QUIET_MAX = 2           # panel says "quiet" up to this many in line…
QUEUE_BUSY_MIN = 8            # …and "busy" from this many
_MISSED_GATE_MARKERS = ("failed to arrive", "request has been removed", "timed out", "gate closed")

ACTIVE_STATUSES = ("queued", "preparing", "ready")
TERMINAL_STATUSES = ("completed", "cancelled", "error")

WEB_ORDER_URL = f"{WEBSITE_BASE}/order"

_HEX_RE = re.compile(r"(?:[0-9a-fA-F]{4}|[0-9a-fA-F]{16})")
_ENTRY_RE = re.compile(r"^(?P<item>.+?)(?:\s*[x×]\s*(?P<qx>[0-9]+)|\s+(?P<q>[0-9]+))?$", re.IGNORECASE)


def _order_island() -> str:
    return getattr(Config, "ORDER_BOT_ISLAND", "Sinta") or "Sinta"


def _client() -> SysBotClient:
    return get_island_sysbot_client(_order_island(), timeout=10)


def _gate_seconds() -> int:
    """How long the gate stays open after the Dodo code is given. SysBot doesn't report it, so it's configured."""
    return int(getattr(Config, "ORDER_GATE_SECONDS", 0) or 180)


def _bar(done: int, total: int, width: int = 10) -> str:
    """`▰▰▰▱▱▱▱▱▱▱`: a text progress bar. Anything above zero shows at least one block."""
    if total <= 0:
        return "▱" * width
    filled = max(0, min(width, round(width * done / total)))
    if done > 0 and filled == 0:
        filled = 1
    return "▰" * filled + "▱" * (width - filled)


def _embed(title: str, description: str = "", color: int = PANEL_EMBED_COLOR) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=color)
    embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
    return embed


# ─── Pocket model ─────────────────────────────────────────────────────────────

@dataclass
class OrderLine:
    """One row of the pocket. `hex` is set for raw codes and for color/pattern variants."""

    name: str
    qty: int = 1
    hex: str = ""
    stack: int = 1
    override: str = ""   # hand-typed hex for this item; wins over the catalog code when the order is sent

    @property
    def code(self) -> str:
        """The hex that is sent for this line, "" when it goes out by name."""
        return self.override or self.hex

    @property
    def slots(self) -> int:
        """Pocket slots this line takes: a stackable item is one stack, anything else one slot each."""
        return 1 if self.stack > 1 else self.qty

    @property
    def label(self) -> str:
        name = self.name[:1].upper() + self.name[1:]   # the catalog stores some names in lowercase
        return f"{name} ×{self.qty}" if self.qty > 1 else name


@dataclass
class OrderCart:
    lines: list[OrderLine] = field(default_factory=list)
    villager: tuple[str, str] | None = None   # (display name, internal id such as "cat23")

    @property
    def slots(self) -> int:
        return sum(line.slots for line in self.lines)

    @property
    def is_empty(self) -> bool:
        return not self.lines and not self.villager

    def add(self, line: OrderLine) -> str | None:
        """Merge `line` into the pocket. Returns a user-facing error if it doesn't fit."""
        existing = next(
            (l for l in self.lines if (l.name, l.hex, l.override) == (line.name, line.hex, line.override)), None
        )
        if existing and existing.stack > 1:
            existing.qty = min(existing.stack, existing.qty + line.qty)   # still one stack, one slot
            return None
        if self.slots + line.slots > ORDER_MAX_SLOTS:
            return f"**{line.label}** doesn't fit: the pocket holds {ORDER_MAX_SLOTS} slots ({self.slots} used)."
        if existing:
            existing.qty += line.qty
        else:
            self.lines.append(line)
        return None

    def to_json(self) -> str:
        return json.dumps({
            "lines": [[l.name, l.qty, l.hex, l.stack, l.override] for l in self.lines],
            "villager": list(self.villager) if self.villager else None,
        })

    @classmethod
    def from_json(cls, raw: str) -> "OrderCart":
        cart = cls()
        try:
            data = json.loads(raw or "{}")
            for name, qty, hex_code, stack, *rest in data.get("lines") or []:   # older rows have no override
                cart.lines.append(OrderLine(
                    str(name), max(1, int(qty)), str(hex_code or ""), max(1, int(stack)), str(rest[0] or "") if rest else "",
                ))
            villager = data.get("villager")
            if villager and len(villager) == 2:
                cart.villager = (str(villager[0]), str(villager[1]))
        except Exception as exc:
            logger.debug("[OrderBot] Could not restore a saved pocket: %s", exc)
        return cart


def _stack_size(item: CatalogItem) -> int:
    try:
        return max(1, int(item.stack_size or 1))
    except (TypeError, ValueError):
        return 1


def line_from_catalog(item: CatalogItem, qty: int | None = None) -> OrderLine:
    """Pocket line for a catalog search hit. A full stack by default, like the website's quick add."""
    stack = _stack_size(item)
    has_variant = bool(item.variation) and item.variation.lower() not in ("na", "none")
    hex_code = item.to_hex() if has_variant else ""
    if hex_code:
        # A variant hex already uses the count field for the color, so it can't also carry a stack count.
        return OrderLine(item.display_name, max(1, min(qty or 1, ORDER_MAX_SLOTS)), hex_code.upper(), 1)
    qty = max(1, min(qty or stack, stack if stack > 1 else ORDER_MAX_SLOTS))
    return OrderLine(item.name, qty, "", stack)


def _resolve_entry(text: str, qty: int) -> OrderLine | None:
    """Turn typed text into a pocket line: raw hex, exact catalog name, or a named variant ("ABD (Blue)")."""
    text = text.strip()
    if not text:
        return None
    item = ACNHCatalog.get_instance().get_item(text) or cf.find_by_translation(text)
    if item:
        stack = _stack_size(item)   # the English name is kept: it is what SysBot is sent
        return OrderLine(item.name, min(qty, stack) if stack > 1 else qty, "", stack)
    if _HEX_RE.fullmatch(text):
        return OrderLine(text.upper(), qty, text.upper(), 1)
    try:
        from utils.data_manager import resolve_item_hex
        hex_code = resolve_item_hex(text)
    except Exception:
        hex_code = None
    if hex_code:
        return OrderLine(text, qty, hex_code.upper(), 1)
    return None


def parse_order_text(raw: str) -> tuple[list[OrderLine], str | None, list[str]]:
    """
    Parse "Gold nugget 30, Royal crown, 09C9 x10, villager:cat23" (or a pasted `!order ...` hex command).
    Returns (lines, villager query or None, user-facing problems). Blocking: run in a thread.
    """
    items_text, villager = parse_order_input(re.sub(r"[\r\n;]+", ",", raw or ""))
    tokens = items_text.split()
    if tokens and all(_HEX_RE.fullmatch(t) for t in tokens):
        entries = tokens   # a command copied from the website's command builder
    else:
        entries = [e.strip() for e in items_text.split(",") if e.strip()]

    lines: list[OrderLine] = []
    problems: list[str] = []
    for entry in entries:
        # Whole entry first, so names that end in a number ("Marine Song 2001") aren't read as a quantity.
        line = _resolve_entry(entry, 1)
        if line is None:
            m = _ENTRY_RE.match(entry)
            name = m.group("item").strip() if m else entry
            qty = int(m.group("qx") or m.group("q") or 1) if m else 1
            if not 1 <= qty <= ORDER_MAX_SLOTS:
                problems.append(f"Quantity for **{name[:40]}** must be between 1 and {ORDER_MAX_SLOTS}.")
                continue
            line = _resolve_entry(name, qty)
            if line is None:
                hits = ACNHCatalog.get_instance().search_items(name, limit=1)
                hint = f" Did you mean **{hits[0].name}**?" if hits else ""
                problems.append(f"I couldn't find **{name[:40]}**.{hint}")
                continue
        lines.append(line)
    return lines, villager, problems


_HEX_TOKEN_RE = re.compile(r"^(?:0x)?(?P<hex>[0-9a-fA-F]{4}|[0-9a-fA-F]{16})(?:[x×*](?P<qty>\d+))?$", re.IGNORECASE)
_QTY_TOKEN_RE = re.compile(r"^[x×*](?P<qty>\d+)$", re.IGNORECASE)


def parse_hex_command(raw: str) -> tuple[list[OrderLine], str | None, list[str]]:
    """
    Strict parser for the "Custom hex" button: a pasted `!order 14BB 16DB 0000001D000009C9` command or
    bare codes. Only 4- or 16-digit hex codes are accepted (optionally `14BBx3` or `14BB x3`), plus an
    optional `villager:<id>`. Returns (lines, villager query or None, user-facing problems).
    """
    items_text, villager = parse_order_input(re.sub(r"[\r\n;,]+", " ", raw or ""))
    lines: list[OrderLine] = []
    problems: list[str] = []
    for token in items_text.split():
        repeat = _QTY_TOKEN_RE.match(token)
        if repeat and lines:
            qty = int(repeat.group("qty"))   # "14BB x3": the count applies to the code before it
            if 1 <= qty <= ORDER_MAX_SLOTS:
                lines[-1].qty = qty
            else:
                problems.append(f"Quantity for `{lines[-1].hex}` must be between 1 and {ORDER_MAX_SLOTS}.")
            continue
        m = _HEX_TOKEN_RE.match(token)
        if not m:
            problems.append(f"`{token[:24]}` isn't a hex code. Codes are 4 or 16 hex digits, like `14BB`.")
            continue
        qty = int(m.group("qty") or 1)
        if not 1 <= qty <= ORDER_MAX_SLOTS:
            problems.append(f"Quantity for `{m.group('hex').upper()}` must be between 1 and {ORDER_MAX_SLOTS}.")
            continue
        code = m.group("hex").upper()
        lines.append(OrderLine(code, qty, code, 1))
    if not lines and not villager and not problems:
        problems.append("Enter at least one hex code.")
    return lines, villager, problems


def build_order_text(lines: list[OrderLine]) -> str:
    """
    The `order` string for SysBot's POST /api/order.

    Plain catalog names are sent as a name list ("Gold nugget 30, Royal crown"). As soon as one line
    needs a hex code (a variant or a raw code) the whole order is sent as hex, because SysBot reads
    an order as either names or hex, not a mix.
    """
    if all(not line.code for line in lines):
        parts: list[str] = []
        for line in lines:
            if line.stack > 1 and line.qty > 1:
                parts.append(f"{line.name} {line.qty}")
            else:
                parts.extend([line.name] * line.qty)   # a trailing " 1" makes NHSE's parser fail
        return ", ".join(parts)

    from utils.data_manager import resolve_item_hex
    tokens: list[str] = []
    for line in lines:
        hex_code = line.code or resolve_item_hex(line.name) or ""
        if not hex_code:
            raise ValueError(f"No item code found for {line.name}.")
        if line.stack > 1 and line.qty > 1 and len(hex_code) == 4:
            tokens.append(f"{line.qty - 1:08X}0000{hex_code.upper()}")   # count field holds quantity - 1
        else:
            tokens.extend([hex_code.upper()] * line.qty)
    return " ".join(tokens)


_BULK_ROW_RE = re.compile(r"^\s*(\d+)\s*[.)]\s*.*?[:=]\s*(\d+)\s*$")


def bulk_edit_text(cart: OrderCart, limit: int = 3800) -> str:
    """The order as "N. Item: quantity" rows for the bulk edit box (as many rows as fit)."""
    rows: list[str] = []
    used = 0
    for i, line in enumerate(cart.lines, 1):
        row = f"{i}. {line.name[:60]}: {line.qty}"
        if used + len(row) + 1 > limit:
            break
        rows.append(row)
        used += len(row) + 1
    return "\n".join(rows)


def apply_bulk_edit(cart: OrderCart, text: str) -> str:
    """
    Apply edited "N. Item: quantity" rows to the order. 0 removes the item; rows left out are kept as
    they are. Quantities are capped at one stack (stackable) or at the free slots. Returns a notice.
    """
    wanted: dict[int, int] = {}
    problems: list[str] = []
    for raw in (text or "").splitlines():
        if not raw.strip():
            continue
        m = _BULK_ROW_RE.match(raw)
        if not m or not 1 <= int(m.group(1)) <= len(cart.lines):
            problems.append(f"Couldn't read `{raw.strip()[:40]}` — keep the `N. Item: quantity` format.")
            continue
        wanted[int(m.group(1)) - 1] = int(m.group(2))

    changed = 0
    for idx, qty in wanted.items():
        line = cart.lines[idx]
        if qty <= 0:
            continue
        if line.stack > 1:
            new = min(qty, line.stack)
            if new < qty:
                problems.append(f"**{line.name}** capped at {new} (one full stack).")
        else:
            # One slot each: whatever the rest of the order (minus removed rows) leaves free.
            others = sum(l.slots for j, l in enumerate(cart.lines) if j != idx and wanted.get(j, 1) > 0)
            new = max(1, min(qty, ORDER_MAX_SLOTS - others))
            if new < qty:
                problems.append(f"**{line.name}** capped at {new}: an order holds {ORDER_MAX_SLOTS} slots.")
        changed += new != line.qty
        line.qty = new
    removed = [idx for idx, qty in wanted.items() if qty <= 0]
    for idx in sorted(removed, reverse=True):
        cart.lines.pop(idx)

    parts = []
    if changed:
        parts.append(f"Updated {changed} quantit{'ies' if changed != 1 else 'y'}.")
    if removed:
        parts.append(f"Removed {len(removed)} item{'s' if len(removed) != 1 else ''}.")
    return "\n".join((parts or ["Nothing changed."]) + problems[:5])[:1000]


def cart_summary(cart: OrderCart, limit: int = 120) -> str:
    """Short human-readable description of a pocket, used in the tracker and in DMs."""
    parts = [line.label for line in cart.lines]
    if cart.villager:
        parts.append(f"🏠 {cart.villager[0]}")
    text = ", ".join(parts) or "—"
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _base_name(line: OrderLine) -> str:
    """"Ironwood dresser (Oak)" -> "Ironwood dresser" for a colour/pattern variant; the name otherwise."""
    if line.hex and line.name.endswith(")") and " (" in line.name:
        return line.name.rsplit(" (", 1)[0]
    return line.name


def _pocket_counts(cart: OrderCart) -> dict[str, int]:
    """Lowercased item name -> how many are in the pocket (all colours together)."""
    counts: dict[str, int] = {}
    for line in cart.lines:
        key = _base_name(line).lower()
        counts[key] = counts.get(key, 0) + line.qty
    return counts


def _line_item(line: OrderLine) -> CatalogItem | None:
    """The catalog record behind a pocket line; None for raw hex codes."""
    catalog = ACNHCatalog.get_instance()
    for name in dict.fromkeys((_base_name(line), line.name)):
        item = catalog.get_item(name)
        if item:
            return item
    return None


def _line_image(line: OrderLine) -> str:
    """Picture of a pocket line, in its chosen colour/pattern when it has one."""
    item = _line_item(line)
    if item is None:
        return ""
    if line.hex:
        variant = next((v for v in _item_variants(item) if v.item.to_hex().upper() == line.hex), None)
        if variant and variant.item.image_url:
            return variant.item.image_url
    return item.image_url


_HEX_NAMES: dict[str, tuple[str, int]] = {}   # hex code -> (item name, stack size), built on first use


def _hex_item(code: str) -> tuple[str, int] | None:
    """(name, stack size) for an order hex code. 16-digit codes that aren't a known look use their base id."""
    if not _HEX_NAMES:
        for item in ACNHCatalog.get_instance().items:
            try:
                hex_code = item.to_hex().upper()
                shown = item.display_name
            except (AttributeError, TypeError, ValueError):   # a few catalog rows have a numeric variation
                continue
            if hex_code:
                _HEX_NAMES.setdefault(hex_code, (shown, _stack_size(item)))
                _HEX_NAMES.setdefault(hex_code[-4:], (item.name, _stack_size(item)))
    code = code.upper()
    return _HEX_NAMES.get(code) or (_HEX_NAMES.get(code[-4:]) if len(code) == 16 else None)


def readable_command(command: str, limit: int = 90) -> str:
    """A stored order command in words: "Gold nugget ×30, Royal crown" instead of raw hex codes."""
    def short(text: str) -> str:
        return text if len(text) <= limit else text[: limit - 1] + "…"

    items_text, villager = parse_order_input(command or "")
    tokens = items_text.split()
    if not tokens or not all(_HEX_RE.fullmatch(t) for t in tokens):
        return short(re.sub(r"^!(order|drop)\s*", "", (command or "").strip(), flags=re.IGNORECASE))
    counts: dict[str, int] = {}
    for token in tokens:
        known = _hex_item(token)
        if known is None:
            name, qty = token.upper(), 1
        else:
            name, stack = known
            exact = _HEX_NAMES.get(token.upper())
            # A stack sent as hex carries "quantity - 1" in its first 8 digits.
            qty = int(token[:8], 16) + 1 if len(token) == 16 and not exact and stack > 1 else 1
        name = name[:1].upper() + name[1:]   # the catalog stores some names in lowercase
        counts[name] = counts.get(name, 0) + qty
    parts = [f"{name} ×{qty}" if qty > 1 else name for name, qty in counts.items()]
    if villager:
        parts.append(f"🏠 {villager}")
    return short(", ".join(parts))


# ─── Database helpers (synchronous; call through asyncio.to_thread) ───────────

def _init_tables() -> None:
    try:
        with connect_db() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS order_bot_panels (
                    channel_id VARCHAR(64) PRIMARY KEY,
                    message_id VARCHAR(64),
                    updated_at INTEGER
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS order_bot_carts (
                    user_id    VARCHAR(64) PRIMARY KEY,
                    payload    TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS order_bot_loadouts (
                    user_id    VARCHAR(64) NOT NULL,
                    name       VARCHAR(64) NOT NULL,
                    payload    TEXT NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (user_id, name)
                )
                """
            )
            # Orders placed from Discord: lets tracking (and the Dodo DM) resume after a restart.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS order_bot_discord_orders (
                    order_id   VARCHAR(255) PRIMARY KEY,
                    user_id    VARCHAR(64) NOT NULL,
                    summary    TEXT,
                    dm_sent    INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL
                )
                """
            )
            # Members waiting for a DM when the Order Bot opens again.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS order_bot_notify (
                    user_id    VARCHAR(64) PRIMARY KEY,
                    created_at INTEGER NOT NULL
                )
                """
            )
            conn.commit()
    except Exception as exc:
        logger.warning("[OrderBot] Could not create tables: %s", exc)
    # Columns added after the first release; each in its own connection because it fails when present.
    for column in ("cart TEXT", "next_sent INTEGER NOT NULL DEFAULT 0"):
        with contextlib.suppress(Exception):
            with connect_db() as conn:
                conn.execute(f"ALTER TABLE order_bot_discord_orders ADD COLUMN {column}")
                conn.commit()


def _db_run(sql: str, params: tuple = ()) -> None:
    try:
        with connect_db() as conn:
            conn.execute(sql, params)
            conn.commit()
    except Exception as exc:
        logger.warning("[OrderBot] DB write failed: %s", exc)


def _db_rows(sql: str, params: tuple = ()) -> list[Any]:
    try:
        with connect_db() as conn:
            return list(conn.execute(sql, params).fetchall())
    except Exception as exc:
        logger.warning("[OrderBot] DB read failed: %s", exc)
        return []


def _load_panels() -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for row in _db_rows("SELECT channel_id, message_id FROM order_bot_panels"):
        with contextlib.suppress(TypeError, ValueError):
            out.append((int(row["channel_id"]), int(row["message_id"])))
    return out


def _save_panel(channel_id: int, message_id: int) -> None:
    _db_run("DELETE FROM order_bot_panels WHERE channel_id = ?", (str(channel_id),))
    _db_run(
        "INSERT INTO order_bot_panels (channel_id, message_id, updated_at) VALUES (?, ?, ?)",
        (str(channel_id), str(message_id), int(time.time())),
    )


def _delete_panel(channel_id: int) -> None:
    _db_run("DELETE FROM order_bot_panels WHERE channel_id = ?", (str(channel_id),))


def _load_cart(user_id: int) -> OrderCart:
    rows = _db_rows("SELECT payload FROM order_bot_carts WHERE user_id = ?", (str(user_id),))
    return OrderCart.from_json(rows[0]["payload"]) if rows else OrderCart()


def _save_cart(user_id: int, cart: OrderCart) -> None:
    _db_run("DELETE FROM order_bot_carts WHERE user_id = ?", (str(user_id),))
    if not cart.is_empty:
        _db_run(
            "INSERT INTO order_bot_carts (user_id, payload, updated_at) VALUES (?, ?, ?)",
            (str(user_id), cart.to_json(), int(time.time())),
        )


def _load_loadouts(user_id: int) -> list[tuple[str, OrderCart]]:
    rows = _db_rows(
        "SELECT name, payload FROM order_bot_loadouts WHERE user_id = ? ORDER BY updated_at DESC",
        (str(user_id),),
    )
    return [(row["name"], OrderCart.from_json(row["payload"])) for row in rows]


def _save_loadout(user_id: int, name: str, cart: OrderCart) -> None:
    _db_run("DELETE FROM order_bot_loadouts WHERE user_id = ? AND name = ?", (str(user_id), name))
    _db_run(
        "INSERT INTO order_bot_loadouts (user_id, name, payload, updated_at) VALUES (?, ?, ?, ?)",
        (str(user_id), name, cart.to_json(), int(time.time())),
    )


def _delete_loadout(user_id: int, name: str) -> None:
    _db_run("DELETE FROM order_bot_loadouts WHERE user_id = ? AND name = ?", (str(user_id), name))


def _active_order_row(user_id: int) -> dict | None:
    """The member's newest order the DB still believes is in progress."""
    placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
    rows = _db_rows(
        f"""
        SELECT id, command, status FROM order_bot_queue
        WHERE user_id = ? AND status IN ({placeholders}) AND created_at > ?
        ORDER BY created_at DESC LIMIT 1
        """,
        (str(user_id), *ACTIVE_STATUSES, int(time.time()) - ACTIVE_ORDER_WINDOW),
    )
    if not rows:
        return None
    return {"id": rows[0]["id"], "command": rows[0]["command"] or "", "status": rows[0]["status"]}


def _record_discord_order(order_id: str, user_id: int, summary: str, cart_json: str = "") -> None:
    _db_run("DELETE FROM order_bot_discord_orders WHERE order_id = ?", (order_id,))
    _db_run(
        "INSERT INTO order_bot_discord_orders (order_id, user_id, summary, dm_sent, created_at, cart) VALUES (?, ?, ?, 0, ?, ?)",
        (order_id, str(user_id), summary, int(time.time()), cart_json),
    )


def _cart_from_command(command: str) -> OrderCart:
    """Rebuild a pocket from a stored order command (names or hex, optional villager:<id>)."""
    lines, villager, _ = parse_order_text(command)
    cart = OrderCart()
    for line in lines:
        cart.add(line)
    if villager:
        found = ACNHCatalog.get_instance().get_villager(villager)
        if found:
            cart.villager = (found.name, found.villager_id)
    return cart


def _order_cart(order_id: str) -> OrderCart:
    """The pocket of a past order: the saved pocket for Discord orders, else parsed from its command."""
    rows = _db_rows("SELECT cart FROM order_bot_discord_orders WHERE order_id = ?", (order_id,))
    if rows and rows[0]["cart"]:
        cart = OrderCart.from_json(rows[0]["cart"])
        if not cart.is_empty:
            return cart
    rows = _db_rows("SELECT command FROM order_bot_queue WHERE id = ?", (order_id,))
    return _cart_from_command(rows[0]["command"] or "") if rows else OrderCart()


def history_labels(orders: list[dict]) -> dict[str, str]:
    """order id -> what was ordered, in words: the saved summary for Discord orders, else the command decoded."""
    ids = [str(o.get("id")) for o in orders if o.get("id")]
    summaries: dict[str, str] = {}
    if ids:
        placeholders = ",".join("?" for _ in ids)
        for row in _db_rows(f"SELECT order_id, summary FROM order_bot_discord_orders WHERE order_id IN ({placeholders})", tuple(ids)):
            if row["summary"]:
                summaries[str(row["order_id"])] = row["summary"]
    return {
        str(o.get("id")): summaries.get(str(o.get("id"))) or readable_command(str(o.get("command") or ""))
        for o in orders
    }


def _recent_order_names(user_id: int, limit: int = RECENT_ITEMS) -> list[str]:
    """Items the member ordered from Discord lately, newest first, without duplicates."""
    rows = _db_rows(
        "SELECT cart FROM order_bot_discord_orders WHERE user_id = ? AND cart IS NOT NULL ORDER BY created_at DESC LIMIT 10",
        (str(user_id),),
    )
    names: dict[str, str] = {}
    for row in rows:
        for line in OrderCart.from_json(row["cart"]).lines:
            name = _base_name(line)
            names.setdefault(name.lower(), name)
            if len(names) >= limit:
                return list(names.values())
    return list(names.values())


def _next_sent(order_id: str) -> bool:
    rows = _db_rows("SELECT next_sent FROM order_bot_discord_orders WHERE order_id = ?", (order_id,))
    return bool(rows and rows[0]["next_sent"])


def _mark_next_sent(order_id: str) -> None:
    _db_run("UPDATE order_bot_discord_orders SET next_sent = 1 WHERE order_id = ?", (order_id,))


def _add_subscriber(user_id: int) -> None:
    _db_run("DELETE FROM order_bot_notify WHERE user_id = ?", (str(user_id),))
    _db_run("INSERT INTO order_bot_notify (user_id, created_at) VALUES (?, ?)", (str(user_id), int(time.time())))


def _pop_subscribers() -> list[int]:
    """Everyone waiting for the "open again" DM, oldest first. Clears the list."""
    rows = _db_rows("SELECT user_id FROM order_bot_notify ORDER BY created_at")
    if rows:
        _db_run("DELETE FROM order_bot_notify")
    out: list[int] = []
    for row in rows:
        with contextlib.suppress(TypeError, ValueError):
            out.append(int(row["user_id"]))
    return out


def _discord_order_info(order_id: str) -> tuple[str, bool]:
    """(summary, dodo already DMed) for an order placed from Discord; ("", False) for anything else."""
    rows = _db_rows("SELECT summary, dm_sent FROM order_bot_discord_orders WHERE order_id = ?", (order_id,))
    if not rows:
        return "", False
    return rows[0]["summary"] or "", bool(rows[0]["dm_sent"])


def _mark_dm_sent(order_id: str) -> None:
    _db_run("UPDATE order_bot_discord_orders SET dm_sent = 1 WHERE order_id = ?", (order_id,))


def _resumable_orders() -> list[tuple[str, int, str]]:
    """Discord-placed orders that were still in progress when the bot last stopped."""
    placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
    rows = _db_rows(
        f"""
        SELECT d.order_id, d.user_id, d.summary FROM order_bot_discord_orders d
        JOIN order_bot_queue q ON q.id = d.order_id
        WHERE q.status IN ({placeholders}) AND d.created_at > ?
        """,
        (*ACTIVE_STATUSES, int(time.time()) - TRACK_MAX_SECONDS),
    )
    out: list[tuple[str, int, str]] = []
    for row in rows:
        with contextlib.suppress(TypeError, ValueError):
            out.append((row["order_id"], int(row["user_id"]), row["summary"] or ""))
    return out


# ─── Access ───────────────────────────────────────────────────────────────────

def _is_staff(user: discord.abc.User) -> bool:
    perms = getattr(user, "guild_permissions", None)
    return bool(perms and (perms.administrator or perms.manage_guild))


async def _order_username(user: discord.abc.User) -> str | None:
    """The "Character | Island" name SysBot shows in-game: the nickname, else the saved character."""
    nick = getattr(user, "display_name", "") or ""
    if is_valid_acnh_nickname(nick):
        return nick
    saved = await asyncio.to_thread(_lookup_saved_character, str(user.id))
    if saved:
        ign, island = saved
        return f"{ign} | {island}" if island else ign
    return None


def _link_view(*links: tuple[str, str, str]) -> discord.ui.View:
    """A row of link buttons: (label, emoji, url)."""
    view = discord.ui.View(timeout=None)
    for label, emoji, url in links:
        view.add_item(discord.ui.Button(label=label, emoji=emoji, style=discord.ButtonStyle.link, url=url))
    return view


def _channel_url(interaction: discord.Interaction, channel_id: int) -> str:
    guild_id = interaction.guild_id or getattr(Config, "GUILD_ID", 0) or 0
    return f"https://discord.com/channels/{guild_id}/{channel_id}"


def _order_rules_channel_id() -> int:
    return getattr(Config, "ORDER_RULES_CHANNEL_ID", 0) or 1262585130397208636   # #chorder-rules


def _nickname_channel_id() -> int:
    return getattr(Config, "NICKNAME_CHANNEL_ID", 0) or 1081147108612124742


async def _order_gate(interaction: discord.Interaction) -> str | None:
    """Username to order under if this member may order; otherwise replies with the reason and returns None."""
    user = interaction.user
    staff = _is_staff(user)
    if not staff:
        denied = await _denied_reason(user.id)
        if denied:
            await _reply(interaction, embed=_err_embed(denied))
            return None
        if not await asyncio.to_thread(_has_order_role, user):
            rules_id = _order_rules_channel_id()
            await _reply(
                interaction,
                embed=_err_embed(
                    "You need to **accept the Order Bot rules** before ordering.",
                    hint=f"Read the rules in <#{rules_id}> and press **Done!** there, then come back and try again.",
                ),
                view=_link_view(("Go to the order rules", "📜", _channel_url(interaction, rules_id))),
            )
            return None
    username = await _order_username(user)
    if not username and not staff:
        await _reply(
            interaction,
            embed=_err_embed(
                "The Order Bot needs to know who is flying in.",
                hint=(
                    "Set your server nickname to `Character Name | Island Name`, "
                    "or register your in-game character on the website. Use a button below."
                ),
            ),
            view=_link_view(
                ("Set my nickname", "✏️", _channel_url(interaction, _nickname_channel_id())),
                ("Register my character", "🌐", f"{WEBSITE_BASE}/profile"),
            ),
        )
        return None
    return username or user.display_name


# ─── Status helpers and embeds ────────────────────────────────────────────────

_UNREACHABLE_MARKERS = ("unreachable", "offline", "timed out", "not configured", "non-json")


def _is_unreachable(data: Any) -> bool:
    """SysBot didn't answer. (normalize_order_data fills in status="queued" even then, so check this first.)"""
    if not isinstance(data, dict):
        return True
    if data.get("success") is not False:
        return False
    error = str(data.get("error") or "").lower()
    return any(marker in error for marker in _UNREACHABLE_MARKERS)


def _bot_accepting(status: dict) -> bool:
    island = str(status.get("island_name") or "").strip().lower().rstrip(".")
    if island == "no town name yet" or status.get("success") is False:
        return False
    return bool(status.get("is_running", True)) and bool(status.get("accepting_commands", True))


def _friendly_message(raw: str | None) -> str:
    """Port of the website's formatOrderMessage: SysBot console text → member-friendly text."""
    msg = (raw or "").strip()
    lower = msg.lower()
    if "failed to arrive" in lower or "request has been removed" in lower:
        return "Flight gate expired: the arrival window ended. You can reorder anytime."
    if "order cancelled" in lower or "cancelled by user" in lower:
        return "Your order was cancelled."
    if "pickup completed" in lower or "order completed" in lower:
        return "Pickup completed. Thank you for flying with Dodo Airlines!"
    if "timed out" in lower or "gate closed" in lower:
        return "The flight gate has closed. Reorder to get a new pickup slot."
    return msg[:300]


def _missed_gate(data: dict) -> bool:
    """The order ended because the member didn't fly in before the gate closed."""
    message = str(data.get("message") or "").lower()
    return any(marker in message for marker in _MISSED_GATE_MARKERS)


_STEPS = ("Queued", "Preparing", "Gate open", "Picked up")
_STEP_INDEX = {"queued": 0, "preparing": 1, "ready": 2, "completed": 3}


def build_order_embed(data: dict, order_id: str, summary: str = "", *, connection_lost: bool = False) -> discord.Embed:
    """Tracker embed for one order in any state."""
    status = str(data.get("status") or "queued")
    island = data.get("island_name") or _order_island()
    position = data.get("queue_position")
    minutes = data.get("estimated_minutes")
    dodo = data.get("dodo_code")
    note = _friendly_message(data.get("message"))

    if status == "ready" and dodo:
        closes = data.get("gate_closes_at")
        deadline = f"⏱️ Gate closes about **<t:{int(closes)}:R>** — fly in now.\n" if closes else ""
        embed = _embed(
            f"✈️ Gate open — fly to {island}!",
            f"# `{dodo}`\n"
            f"{deadline}"
            "Open your gate at the airport → **I want to fly** → **Via Dodo code**.\n"
            "-# ⚠️ This code is yours only. Pick up your items, then leave through the airport.",
        )
        embed.set_thumbnail(url=DODO_GIF)
    elif status == "preparing":
        embed = _embed("🛠️ You're up next!", f"**{island}** is setting out your order. Your Dodo code arrives any moment — get to your airport.")
    elif status == "completed":
        embed = _embed("✅ Order complete", note or "Pickup completed. Thank you for flying with Dodo Airlines!")
    elif status == "cancelled":
        embed = _embed("🚫 Order cancelled", note or "Your order was cancelled.", PANEL_EMBED_COLOR_WARN)
    elif status == "error":
        embed = _embed("⚠️ Order ended", note or "The Order Bot no longer has this order. You can place it again.", PANEL_EMBED_COLOR_ERR)
    else:
        where = f"Position **#{position}** in line" if position else "In line"
        eta = f" · about **{minutes} min**" if minutes else ""
        lines = [f"{where}{eta}."]
        if isinstance(position, int) and position > 0:
            ahead = position - 1
            start = max(int(data.get("start_position") or 0), position)
            people = "Nobody is ahead of you" if ahead == 0 else f"**{ahead}** {'person is' if ahead == 1 else 'people are'} ahead of you"
            bar = f"`{_bar(start - position, start - 1)}` " if start > 1 else ""
            lines.append(f"{bar}{people}.")
        lines.append("Your Dodo code will be sent to your DMs when it's your turn.")
        embed = _embed("⏳ Order in the queue", "\n".join(lines))
        serving = data.get("now_serving")
        if serving:
            embed.add_field(name="Now serving", value=discord.utils.escape_markdown(str(serving))[:60], inline=True)

    if status in _STEP_INDEX:
        current = _STEP_INDEX[status]
        steps = [f"**{s}**" if i == current else (f"~~{s}~~" if i < current else s) for i, s in enumerate(_STEPS)]
        embed.add_field(name="Progress", value=" → ".join(steps), inline=False)
    if summary:
        embed.add_field(name="🎒 Pocket", value=summary[:1000], inline=False)
    if note and status in ACTIVE_STATUSES:
        embed.add_field(name="Order Bot says", value=note, inline=False)
    if connection_lost:
        embed.add_field(name="📡 Connection", value="Can't reach the Order Bot right now. Still trying…", inline=False)
    embed.set_footer(text=f"{PANEL_FOOTER_TEXT} • Order {order_id}", icon_url=PANEL_FOOTER_ICON)
    return embed


def slots_field(cart: OrderCart) -> str:
    """`**12** / 40` with a bar under it, and a warning once the pocket is nearly full."""
    left = ORDER_MAX_SLOTS - cart.slots
    text = f"**{cart.slots}** / {ORDER_MAX_SLOTS}\n`{_bar(cart.slots, ORDER_MAX_SLOTS)}`"
    if cart.slots and left <= 0:
        text += "\n⚠️ Pocket full"
    elif cart.slots and left <= SLOTS_WARN_AT:
        text += f"\n⚠️ {left} slot{'s' if left != 1 else ''} left"
    return text


def pocket_rows(cart: OrderCart) -> list[str]:
    """
    The pocket as numbered rows. Bigger pockets are grouped under category headings; the numbers stay
    the pocket order so they match the bulk "Edit quantities" box.
    """
    numbered = [(i, line) for i, line in enumerate(cart.lines, 1)]
    if len(numbered) >= GROUP_MIN_LINES:
        groups: dict[str, list[tuple[int, OrderLine]]] = {}
        for i, line in numbered:
            item = _line_item(line)
            groups.setdefault(item.category if item and item.category else "Other", []).append((i, line))
        if len(groups) > 1:
            rows: list[str] = []
            for category, members in groups.items():
                rows.append(f"**{category}**")
                rows += [f"`{i:02}` {line.label}" for i, line in members]
            return rows
    return [f"`{i:02}` {line.label}" for i, line in numbered]


def build_pocket_embed(cart: OrderCart, username: str, notice: str | None = None, status_line: str | None = None) -> discord.Embed:
    """The order screen. Always tells the member what to do next."""
    embed = _embed("🛒 Your order")
    if cart.is_empty:
        embed.description = (
            "**How to order**\n"
            "1️⃣ Press **Find items** and pick what you want (or grab a ready-made **Bundle**)\n"
            "2️⃣ Press **Place order**\n"
            "3️⃣ I'll DM you a Dodo code when it's your turn — fly in and pick everything up"
        )
    else:
        text = "\n".join(pocket_rows(cart))
        if len(text) > 3400:
            text = text[:3400].rsplit("\n", 1)[0] + "\n…"
        embed.description = (text + "\n\n" if text else "") + "👉 Happy with it? Press **Place order**. Tap an item in the menu to change it."
        # The newest item's picture, so the screen shows what was just added.
        image = next((url for url in (_line_image(line) for line in reversed(cart.lines[-5:])) if url), "")
        if image:
            embed.set_thumbnail(url=image)
    embed.add_field(name="Slots", value=slots_field(cart), inline=True)
    embed.add_field(name="🏠 Villager", value=cart.villager[0] if cart.villager else "None", inline=True)
    embed.add_field(name="Ordering as", value=discord.utils.escape_markdown(username)[:100], inline=True)
    if status_line:
        embed.add_field(name="Order Bot", value=status_line, inline=False)
    if notice:
        embed.add_field(name="ℹ️", value=notice[:1000], inline=False)
    return embed


def build_review_embed(cart: OrderCart, username: str, status: dict, queue_data: Any) -> discord.Embed:
    """Last look before the order is sent: everything in it, where it goes, and how long the wait is."""
    island = _order_island()
    embed = _embed("✈️ Check your order", f"This pocket goes to **{island}**. Press **Send order** to join the queue.")
    text = "\n".join(pocket_rows(cart)) or "—"
    if len(text) > 3000:
        text = text[:3000].rsplit("\n", 1)[0] + "\n…"
    embed.description += "\n\n" + text
    embed.add_field(name="Slots", value=slots_field(cart), inline=True)
    embed.add_field(name="🏠 Villager", value=cart.villager[0] if cart.villager else "None", inline=True)
    embed.add_field(name="Flying in as", value=discord.utils.escape_markdown(username)[:100], inline=True)
    if _bot_accepting(status):
        entries = _queue_entries(queue_data)
        waiting = len(entries) or int(status.get("queue_count") or 0)
        wait = _queue_wait(entries)
        line = f"🟢 Open · **{waiting}** in line" + (f" · about **{wait} min** wait" if wait else "") if waiting else "🟢 Open · no wait, you'd be first"
    else:
        line = "🔴 Not taking orders right now. Your pocket stays saved, so try again in a bit."
    embed.add_field(name="Order Bot", value=line, inline=False)
    embed.add_field(
        name="Before you send",
        value="• Keep your **DMs open**, that's where the Dodo code goes\n• Make room in your **in-game pockets**\n"
              + ("• Have an **empty plot** for your villager\n" if cart.villager else "")
              + "• Fly in quickly once the gate opens",
        inline=False,
    )
    return embed


def build_guide_embed() -> discord.Embed:
    island = _order_island()
    embed = _embed(
        "📖 How the Order Bot works",
        f"Order up to **{ORDER_MAX_SLOTS} pocket slots** of items (plus one villager) and pick them up on **{island}**.",
    )
    embed.add_field(
        name="1 · Choose your items",
        value=(
            "Press **Start Order**, then **Find items** and pick what you want from the list. "
            "You can also grab a ready-made **Bundle** or type a list of items."
        ),
        inline=False,
    )
    embed.add_field(
        name="2 · Place the order",
        value="You join the queue. The tracker shows your position and estimated wait and updates on its own.",
        inline=False,
    )
    embed.add_field(
        name="3 · Fly in",
        value=(
            "When it's your turn the bot **DMs your Dodo code** (keep DMs open). "
            "Fly in quickly: the gate only waits a short time. Grab your items and leave through the airport."
        ),
        inline=False,
    )
    embed.add_field(
        name="Rules",
        value=(
            "• Nickname must be `Character | Island`\n"
            "• One order at a time\n"
            "• Never share your Dodo code\n"
            f"• Prefer a bigger screen? Use the [Web Order Bot]({WEB_ORDER_URL})"
        ),
        inline=False,
    )
    return embed


def _queue_entries(queue_data: Any) -> list[dict]:
    if not isinstance(queue_data, dict):
        return []
    entries = queue_data.get("queue") or queue_data.get("orders") or []
    return [e for e in entries if isinstance(e, dict)]


def _queue_wait(entries: list[dict]) -> int | None:
    """Rough wait in minutes for an order placed now (the last ETA in line plus a little), or None."""
    etas = [int(e["estimated_minutes"]) for e in entries if str(e.get("estimated_minutes") or "").isdigit()]
    return max(etas) + 2 if etas else None


def _busy_label(waiting: int) -> str:
    if waiting <= QUEUE_QUIET_MAX:
        return "quiet"
    if waiting >= QUEUE_BUSY_MIN:
        return f"busy ({waiting} in line)"
    return "steady"


def status_line(status: dict, queue_data: Any) -> str:
    """One line for the order screen: is the bot open, and how long is the line."""
    entries = _queue_entries(queue_data)
    waiting = len(entries) or int(status.get("queue_count") or 0)
    if not _bot_accepting(status):
        return "🔴 Not taking orders right now. You can still build your pocket."
    if not waiting:
        return "🟢 Open · no wait"
    wait = _queue_wait(entries)
    return f"🟢 Open · **{waiting}** in line" + (f" · about **{wait} min** wait" if wait else "")


def build_queue_embed(queue_data: Any, viewer: discord.abc.User | None = None) -> discord.Embed:
    entries = _queue_entries(queue_data)
    if not entries:
        if isinstance(queue_data, dict) and queue_data.get("success") is False and queue_data.get("error"):
            return _embed("📋 Order queue", "The Order Bot isn't reachable right now.", PANEL_EMBED_COLOR_WARN)
        return _embed("📋 Order queue", "The queue is empty — order now and you're first in line!")
    viewer_id = str(viewer.id) if viewer else ""
    viewer_name = (getattr(viewer, "display_name", "") or "").lower()
    rows: list[str] = []
    for entry in entries[:20]:
        pos = entry.get("queue_position") or entry.get("position") or "?"
        name = discord.utils.escape_markdown(str(entry.get("username") or "Someone"))[:40]
        status = str(entry.get("status") or "queued")
        icon = {"preparing": "🛠️", "ready": "✈️"}.get(status, "⏳")
        eta = f" · ~{entry['estimated_minutes']} min" if entry.get("estimated_minutes") else ""
        mine = viewer and (str(entry.get("user_id") or "") == viewer_id or str(entry.get("username") or "").lower() == viewer_name)
        rows.append(f"`#{pos}` {icon} {name}{eta}{'  ← **you**' if mine else ''}")
    if len(entries) > 20:
        rows.append(f"…and {len(entries) - 20} more")
    return _embed(f"📋 Order queue ({len(entries)})", "\n".join(rows))


def build_panel_embed(status: dict, queue_data: Any) -> discord.Embed:
    island = _order_island()
    entries = _queue_entries(queue_data)
    waiting = len(entries) or int(status.get("queue_count") or 0)

    if _bot_accepting(status):
        state, color = f"🟢 **Open** — taking orders · {_busy_label(waiting)}", PANEL_EMBED_COLOR
    elif status.get("is_running") and status.get("success") is not False:
        state, color = "🟡 **Paused** — not taking orders right now", PANEL_EMBED_COLOR_WARN
    else:
        state, color = "🔴 **Offline** — check back soon", PANEL_EMBED_COLOR_ERR

    embed = _embed(
        f"{PANEL_TITLE_PREFIX} — {island}",
        "Build a pocket of items, join the queue, and get a **private Dodo code** when it's your turn.\n"
        "Press **Start Order** to begin.",
        color,
    )
    embed.add_field(name="Status", value=state, inline=False)
    if waiting:
        minutes = _queue_wait(entries)
        wait = f" · new orders wait about **{minutes} min**" if minutes else ""
        embed.add_field(name="Queue", value=f"**{waiting}** in line{wait}", inline=True)
    else:
        embed.add_field(name="Queue", value="Empty — no wait", inline=True)
    serving = queue_data.get("current_active_user") if isinstance(queue_data, dict) else None
    if serving:
        embed.add_field(name="Now serving", value=discord.utils.escape_markdown(str(serving))[:60], inline=True)
    return embed


# ─── Views: pocket builder ────────────────────────────────────────────────────

async def _swap(interaction: discord.Interaction, old: discord.ui.View | None, embed: discord.Embed, new: discord.ui.View | None) -> None:
    """Replace the menu on the interaction's message. Stops `old` so its timeout can't clobber `new`."""
    if old is not None:
        old.stop()
    if new is not None:
        _adopt(new, interaction)
    if interaction.response.is_done():
        await interaction.edit_original_response(embed=embed, view=new)
    else:
        await interaction.response.edit_message(embed=embed, view=new)


class PocketView(_OwnedView):
    """
    The order builder ("pocket"). A fresh instance is created for every render.

    Kept deliberately small: Find items · Bundles · Place order, the item list, and one row of
    secondary actions. Saved loadouts, custom hex and Clear live under "More".
    """

    expired_notice = "⌛ This menu expired. Your order is saved — press **Start Order** to continue."

    def __init__(self, cog: "OrderBotCog", user_id: int, username: str, *, page: int = 0, undo: str | None = None):
        super().__init__(user_id, timeout=600)
        self.cog = cog
        self.username = username
        self.undo_snapshot = undo   # the pocket before the last removal; Undo puts it back
        if undo is None:
            self.remove_item(self.undo)
        cart = cog.cart(user_id)
        self.place_order.disabled = cart.is_empty
        self.edit_quantities.disabled = not cart.lines
        pages = max(1, math.ceil(len(cart.lines) / POCKET_PAGE_SIZE))
        self.page = max(0, min(page, pages - 1))
        if cart.lines or cart.villager:
            start = self.page * POCKET_PAGE_SIZE
            shown = cart.lines[start:start + POCKET_PAGE_SIZE]
            options = [
                discord.SelectOption(label=f"{i + 1:02}. {line.label}"[:100], value=str(i), emoji="📦")
                for i, line in enumerate(shown, start)
            ]
            if self.page > 0:
                options.insert(0, discord.SelectOption(
                    label=f"Previous items ({max(1, start - POCKET_PAGE_SIZE + 1)}–{start})", value=f"page:{self.page - 1}", emoji="◀️",
                ))
            if self.page < pages - 1:
                end = min(len(cart.lines), start + 2 * POCKET_PAGE_SIZE)
                options.append(discord.SelectOption(
                    label=f"More items ({start + POCKET_PAGE_SIZE + 1}–{end})", value=f"page:{self.page + 1}", emoji="▶️",
                ))
            if cart.villager:
                options.append(discord.SelectOption(label=f"Villager: {cart.villager[0]}"[:100], value="villager", emoji="🏠"))
            placeholder = "Tap an item to change color, quantity, or remove…"
            if pages > 1:
                placeholder = f"Tap an item to change it… (page {self.page + 1}/{pages})"
            select = discord.ui.Select(placeholder=placeholder, options=options, row=1)
            select.callback = self._edit   # type: ignore[method-assign]
            self.add_item(select)

    async def render(self, interaction: discord.Interaction, notice: str | None = None, *, undo: str | None = None) -> None:
        view = PocketView(self.cog, self.user_id, self.username, page=self.page, undo=undo)
        embed = self.cog.pocket_embed(self.user_id, self.username, notice)
        await _swap(interaction, self, embed, view)

    async def _edit(self, interaction: discord.Interaction) -> None:
        cart = self.cog.cart(self.user_id)
        value = (interaction.data or {}).get("values", [""])[0]
        if value.startswith("page:") and value[5:].isdigit():
            self.page = int(value[5:])
            await self.render(interaction)
            return
        if value.isdigit() and int(value) < len(cart.lines):
            view = ItemEditView(self, int(value))
            await _swap(interaction, self, view.embed(), view)
            return
        if value == "villager":
            cart.villager = None
            self.cog.save_cart(self.user_id)
        await self.render(interaction)

    # Row 0 is the whole happy path: find items (or a ready-made bundle), then place the order.
    @discord.ui.button(label="Find items", emoji="🔍", style=discord.ButtonStyle.primary, row=0)
    async def find_items(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        """
        Opens on what the member ordered lately, then the popular items, so nothing has to be typed;
        "Search by name" is one tap away.
        """
        def _load() -> tuple[list[CatalogItem], frozenset[str]]:
            catalog = ACNHCatalog.get_instance()
            recent = [item for item in (catalog.get_item(n) for n in _recent_order_names(self.user_id)) if item]
            names = {item.name.lower() for item in recent}
            popular = [p for p in catalog.get_popular_items(limit=RESULTS_PER_PAGE) if p.name.lower() not in names]
            return (recent + popular)[:RESULTS_PER_PAGE], frozenset(names)

        hits, recent = await asyncio.to_thread(_load)
        view = SearchResultsView(self, hits, "Your recent + popular items" if recent else "Popular items", recent=recent)
        await _swap(interaction, self, view.embed(), view)

    @discord.ui.button(label="Bundles", emoji="🎁", style=discord.ButtonStyle.secondary, row=0)
    async def bundles(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()
        bundles = await self.cog.bundles()
        if not bundles:
            await self.render(interaction, "No bundles are available right now.")
            return
        await _swap(interaction, self, _bundles_embed(bundles), BundleView(self, bundles))

    @discord.ui.button(label="Place order", emoji="✈️", style=discord.ButtonStyle.success, row=0)
    async def place_order(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        """Opens the review screen; the order is sent from there."""
        await self.cog.review_order(interaction, self)

    @discord.ui.button(label="Undo", emoji="↩️", style=discord.ButtonStyle.secondary, row=0)
    async def undo(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        """Only shown right after something was removed: puts the pocket back the way it was."""
        if self.undo_snapshot is not None:
            self.cog.replace_cart(self.user_id, OrderCart.from_json(self.undo_snapshot))
        await self.render(interaction, "↩️ Put back.")

    # Row 2: the less common ways to fill an order.
    @discord.ui.button(label="Type a list", emoji="✍️", style=discord.ButtonStyle.secondary, row=2)
    async def type_items(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(AddItemsModal(self))

    @discord.ui.button(label="Villager", emoji="🏠", style=discord.ButtonStyle.secondary, row=2)
    async def villager(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(VillagerModal(self))

    @discord.ui.button(label="Edit quantities", emoji="🔢", style=discord.ButtonStyle.secondary, row=2)
    async def edit_quantities(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(BulkEditModal(self))

    @discord.ui.button(label="More", emoji="⚙️", style=discord.ButtonStyle.secondary, row=2)
    async def more(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        view = MoreView(self)
        await _swap(interaction, self, view.embed(), view)


async def _show_pocket(
    parent: "PocketView | _SubView", interaction: discord.Interaction, notice: str | None = None, *, undo: str | None = None,
) -> None:
    """Return to the order from wherever a modal was opened (the order itself or one of its sub-menus)."""
    if isinstance(parent, PocketView):
        await parent.render(interaction, notice, undo=undo)
    else:
        await parent.back_to_pocket(interaction, notice, undo=undo)


class AddItemsModal(discord.ui.Modal, title="Type the items you want"):
    items = discord.ui.TextInput(
        label="Items (comma or new line separated)",
        style=discord.TextStyle.paragraph,
        placeholder="Gold nugget 30, Royal crown, Nook Miles Ticket 10\nor paste a command from chopaeng.com",
        max_length=1500,
    )

    def __init__(self, parent: "PocketView | _SubView"):
        super().__init__()
        self.parent = parent

    async def on_submit(self, interaction: discord.Interaction) -> None:
        notice = await self.parent.cog.add_text(self.parent.user_id, self.items.value)
        await _show_pocket(self.parent, interaction, notice)


class BulkEditModal(discord.ui.Modal, title="Edit quantities"):
    """Every quantity in one text box: "N. Item: quantity" per row, 0 removes the item."""

    def __init__(self, parent: "PocketView | _SubView"):
        super().__init__()
        self.parent = parent
        self.body = discord.ui.TextInput(
            label="Change the numbers · 0 removes an item",
            style=discord.TextStyle.paragraph,
            default=bulk_edit_text(parent.cog.cart(parent.user_id)),
            max_length=4000,
        )
        self.add_item(self.body)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cart = self.parent.cog.cart(self.parent.user_id)
        before, count = cart.to_json(), len(cart.lines)
        notice = apply_bulk_edit(cart, self.body.value)
        self.parent.cog.save_cart(self.parent.user_id)
        await _show_pocket(self.parent, interaction, notice, undo=before if len(cart.lines) < count else None)


class ItemHexModal(discord.ui.Modal, title="Custom hex for this item"):
    """Override the code sent for one item. Blank resets it to the catalog code."""

    code = discord.ui.TextInput(
        label="Hex code (4 or 16 characters)",
        placeholder="e.g. 0B64 or 0000002000003A5E (blank = reset)",
        required=False,
        max_length=18,
    )

    def __init__(self, editor: "ItemEditView"):
        super().__init__()
        self.editor = editor
        line = editor.line
        if line and line.override:
            self.code.default = line.override

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.editor.set_override(interaction, self.code.value.strip())


class ItemAmountModal(discord.ui.Modal, title="How many?"):
    """Type an exact quantity instead of tapping + many times."""

    def __init__(self, editor: "ItemEditView"):
        super().__init__()
        self.editor = editor
        line = editor.line
        self.qty = discord.ui.TextInput(
            label=f"Amount (1–{editor._max_qty()})"[:45],
            default=str(line.qty) if line else "1",
            max_length=4,
        )
        self.add_item(self.qty)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.editor.set_amount(interaction, self.qty.value)


class CustomHexModal(discord.ui.Modal, title="Custom command / hex codes"):
    codes = discord.ui.TextInput(
        label="Hex codes or a full !order command",
        style=discord.TextStyle.paragraph,
        placeholder="!order 14BB 16DB 0000001D000009C9\nor: 14BB x3, 09C9, villager:cat23",
        max_length=1500,
    )

    def __init__(self, parent: "PocketView | _SubView"):
        super().__init__()
        self.parent = parent

    async def on_submit(self, interaction: discord.Interaction) -> None:
        notice = await self.parent.cog.add_hex(self.parent.user_id, self.codes.value)
        await _show_pocket(self.parent, interaction, notice)


class SearchModal(discord.ui.Modal, title="Search the catalog"):
    query = discord.ui.TextInput(label="Item name or series", placeholder="ironwood, cherry blossom, crown…", max_length=60)

    def __init__(self, parent: "PocketView | _SubView"):
        super().__init__()
        self.parent = parent

    async def on_submit(self, interaction: discord.Interaction) -> None:
        query = self.query.value.strip()
        language = cf.peek_user_language(self.parent.user_id)
        hits = await asyncio.to_thread(cf.search_items, query, SEARCH_LIMIT, language)
        if not hits:
            notice = f"Nothing found for **{query[:40]}**."
            await _show_pocket(self.parent, interaction, notice)
            return
        view = SearchResultsView(self.parent, hits, f"Results for “{query[:40]}”")
        await _swap(interaction, self.parent, view.embed(), view)


class VillagerModal(discord.ui.Modal, title="Move-in villager"):
    name = discord.ui.TextInput(label="Villager name", placeholder="Raymond", max_length=40)

    def __init__(self, parent: PocketView):
        super().__init__()
        self.parent = parent

    async def on_submit(self, interaction: discord.Interaction) -> None:
        notice = await self.parent.cog.set_villager(self.parent.user_id, self.name.value)
        await self.parent.render(interaction, notice)


class _SubView(_OwnedView):
    """A menu opened from the pocket; "Back" returns to it."""

    expired_notice = PocketView.expired_notice

    def __init__(self, parent: "PocketView | _SubView"):
        super().__init__(parent.user_id, timeout=300)
        self.cog = parent.cog
        self.username = parent.username

    async def back_to_pocket(self, interaction: discord.Interaction, notice: str | None = None, *, undo: str | None = None) -> None:
        view = PocketView(self.cog, self.user_id, self.username, undo=undo)
        await _swap(interaction, self, self.cog.pocket_embed(self.user_id, self.username, notice), view)

    @discord.ui.button(label="Back to my order", emoji="↩️", style=discord.ButtonStyle.secondary, row=4)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.back_to_pocket(interaction)


_CATEGORY_SIZES: dict[str, int] = {}


def _category_choices(present: set[str]) -> list[str]:
    """
    Every catalog category for the category menu, not only the ones in the current results. Discord
    allows 25 options, so with "All categories" there is room for 24: the ones in the results are
    always kept, then the largest of the rest. Shown alphabetically.
    """
    if not _CATEGORY_SIZES:
        catalog = ACNHCatalog.get_instance()
        for item in list(catalog.items) + list(catalog.recipes):
            if item.category:
                _CATEGORY_SIZES[item.category] = _CATEGORY_SIZES.get(item.category, 0) + 1
    others = sorted((c for c in _CATEGORY_SIZES if c not in present), key=lambda c: -_CATEGORY_SIZES[c])
    keep = sorted(present)[:24]
    keep += others[: 24 - len(keep)]
    return sorted(keep)


class SearchResultsView(_SubView):
    """
    Catalog results, DropBot style: 25 per page with ◀ ▶, a category filter, and a shortcut to
    popular series. Adding an item keeps you here so several can be added in a row.
    """

    def __init__(
        self,
        parent: "PocketView | _SubView",
        hits: list[CatalogItem],
        title: str = "Results",
        *,
        page: int = 0,
        category: str | None = None,
        color: str | None = None,
        season: str | None = None,
        whole_category: bool = False,
        kind: str = "all",
        recent: frozenset[str] = frozenset(),
    ):
        super().__init__(parent)
        self.title = title
        self.recent = recent   # lowercased names the member ordered lately (marked 🕘)
        self.in_pocket = _pocket_counts(self.cog.cart(self.user_id))
        self.kind = kind if kind in _KIND_LABELS else "all"   # "all" | "items" | "diy"
        self.all_hits = hits[:CATEGORY_BROWSE_LIMIT]
        self.whole_category = whole_category   # these results are one category loaded from the catalog
        self.present = {h.category for h in self.all_hits if h.category}
        self.category = category if category in self.present else None
        self.color, self.season = color, season
        self.language = cf.peek_user_language(self.user_id)
        self.filters = CatalogFilters(self.category, self.color, self.season)
        self.filtered = self.filters.apply(self.all_hits)
        if self.kind != "all":
            self.filtered = [h for h in self.filtered if bool(h.diy) == (self.kind == "diy")]
        self.pages = max(1, math.ceil(len(self.filtered) / RESULTS_PER_PAGE))
        self.page = max(0, min(page, self.pages - 1))
        self.hits = self.filtered[self.page * RESULTS_PER_PAGE:(self.page + 1) * RESULTS_PER_PAGE]

        if self.hits:
            options = [
                discord.SelectOption(
                    label=cf.localized_name(item, self.language)[:100],
                    value=str(i),
                    description=self._option_description(item),
                    emoji="📜" if item.diy else "📦",
                )
                for i, item in enumerate(self.hits)
            ]
            select = discord.ui.Select(
                placeholder="Pick one item for details, or several to add at once…",
                options=options,
                max_values=min(len(options), MULTI_PICK_MAX),
                row=0,
            )
            select.callback = self._picked   # type: ignore[method-assign]
            self.add_item(select)

        # Every category is offered. One that is in these results filters them; any other one opens
        # that category from the whole catalog.
        self.categories = _category_choices(self.present)
        if len(self.categories) > 1:
            counts = Counter(h.category for h in self.all_hits)
            cat_options = [discord.SelectOption(
                label="All categories", value="__all__", emoji="🗂️",
                default=self.category is None and not self.whole_category,
            )]
            for i, c in enumerate(self.categories):
                in_results = c in self.present and not self.whole_category
                cat_options.append(discord.SelectOption(
                    label=c[:100],
                    value=str(i),
                    description=f"{counts[c]} in these results" if in_results else "Browse this category",
                    default=c == self.category or (self.whole_category and c in self.present),
                ))
            cat_select = discord.ui.Select(placeholder="Choose a category…", options=cat_options, row=1)
            cat_select.callback = self._category_picked   # type: ignore[method-assign]
            self.add_item(cat_select)

        self.series = ACNHCatalog.get_instance().get_popular_series()[:25]
        if self.series:
            series_select = discord.ui.Select(
                placeholder="Browse a popular series…",
                options=[
                    discord.SelectOption(label=label[:100], value=str(i), emoji=emoji or "🌟")
                    for i, (label, _keyword, emoji) in enumerate(self.series)
                ],
                row=2,
            )
            series_select.callback = self._series_picked   # type: ignore[method-assign]
            self.add_item(series_select)

        self.prev_page.disabled = self.page <= 0
        self.next_page.disabled = self.page >= self.pages - 1
        self.place_order.disabled = self.cog.cart(self.user_id).is_empty
        self.toggle_kind.label, self.toggle_kind.emoji = _KIND_LABELS[self.kind]
        n_filters = sum(1 for f in (self.category, self.color, self.season) if f)
        if n_filters:
            self.open_filters.label = f"Filters ({n_filters})"
            self.open_filters.style = discord.ButtonStyle.primary

    def _option_description(self, item: CatalogItem) -> str | None:
        bits = []
        have = self.in_pocket.get(item.name.lower())
        if have:
            bits.append(f"✓ In pocket ×{have}")
        elif item.name.lower() in self.recent:
            bits.append("Ordered before")
        bits.append(item.category or "")
        if _stack_size(item) > 1:
            bits.append(f"stack of {_stack_size(item)}")
        return " · ".join(b for b in bits if b)[:100] or None

    def embed(self, notice: str | None = None) -> discord.Embed:
        total = len(self.filtered)
        described = self.filters.describe()
        scope = f" · {described}" if described else ""
        count = f"**{total}** item{'s' if total != 1 else ''}{scope}"
        if self.pages > 1:
            count += f" · page **{self.page + 1}/{self.pages}**"
        if self.kind != "all":
            count += f" · {_KIND_LABELS[self.kind][0].split(': ')[1]} only"
        cart = self.cog.cart(self.user_id)
        hint = "Pick **one** item to see it and choose quantity or colour, or **several** to add them all at once."
        embed = _embed(f"🔍 {self.title[:80]}", f"{count}\n{hint}")

        # Numbered list of this page, like DropBot's catalog: category, looks, DIY, and what is already ordered.
        if self.hits:
            rows: list[str] = []
            used = 0
            for i, item in enumerate(self.hits, self.page * RESULTS_PER_PAGE + 1):
                have = self.in_pocket.get(item.name.lower())
                mark = f"🛒×{have} " if have else ("🕘 " if item.name.lower() in self.recent else "")
                bits = [f"`{item.category}`"]
                looks = len(_item_variants(item))
                if looks > 1:
                    bits.append(f"🎨 {looks} looks")
                if item.diy:
                    bits.append("DIY")
                row = f"`{i:02d}` {mark}**{cf.localized_name(item, self.language)[:70]}** — {' · '.join(bits)}"
                if used + len(row) + 1 > 3300:
                    rows.append("…")
                    break
                rows.append(row)
                used += len(row) + 1
            embed.description += "\n\n" + "\n".join(rows)
            thumbnail = next((h.image_url for h in self.hits if h.image_url), "")
            if thumbnail:
                embed.set_thumbnail(url=thumbnail)
        embed.add_field(name="🎒 Pocket", value=f"**{cart.slots}** / {ORDER_MAX_SLOTS} slots", inline=True)
        if self.language != cf.DEFAULT_LANGUAGE:
            embed.add_field(name="🌐 Names", value=cf.LANGUAGE_LABELS.get(self.language, self.language), inline=True)
        if notice:
            embed.add_field(name="ℹ️", value=notice[:1000], inline=False)
        return embed

    def fresh(
        self, *, page: int | None = None, category: str | None | bool = False, kind: str | None = None,
    ) -> "SearchResultsView":
        """A new view over the same results (views are single-use). `category=None` clears the filter."""
        return SearchResultsView(
            self,
            self.all_hits,
            self.title,
            page=self.page if page is None else page,
            category=self.category if category is False else category,   # type: ignore[arg-type]
            color=self.color,
            season=self.season,
            whole_category=self.whole_category,
            kind=kind or self.kind,
            recent=self.recent,
        )

    async def _show(self, interaction: discord.Interaction, current: _SubView, view: "SearchResultsView", notice: str | None = None) -> None:
        await _swap(interaction, current, view.embed(notice), view)

    async def add_line_and_stay(self, interaction: discord.Interaction, current: _SubView, line: OrderLine) -> None:
        """Add `line` to the order and show the results again, so several items can be added in a row."""
        error = self.cog.cart(self.user_id).add(line)
        self.cog.save_cart(self.user_id)
        added = f"✅ Added **{line.label}**. Pick more, or press **Place order** when you're done."
        await self._show(interaction, current, self.fresh(), error or added)

    async def _picked(self, interaction: discord.Interaction) -> None:
        picks = [int(v) for v in (interaction.data or {}).get("values", []) if str(v).isdigit()]
        items = [self.hits[i] for i in picks if i < len(self.hits)]
        if len(items) == 1:
            # One item: the detail screen (picture, quantity, colour) before it is added.
            view = ItemEditView(self, draft=line_from_catalog(items[0]), results=self)
            await _swap(interaction, self, view.embed(), view)
            return
        # Several: add a full stack of each in its default look.
        cart = self.cog.cart(self.user_id)
        added: list[str] = []
        problems: list[str] = []
        for item in items:
            line = line_from_catalog(item)
            error = cart.add(line)
            (problems if error else added).append(error or line.label)
        self.cog.save_cart(self.user_id)
        notice = ([f"✅ Added {len(added)} items: {', '.join(added)}."] if added else []) + problems[:3]
        await self._show(interaction, self, self.fresh(), "\n".join(notice)[:1000] or None)

    async def _category_picked(self, interaction: discord.Interaction) -> None:
        value = (interaction.data or {}).get("values", [""])[0]
        category = self.categories[int(value)] if value.isdigit() else None
        catalog = ACNHCatalog.get_instance()
        if category is None and self.whole_category:
            # "All categories" while browsing one category: back to the popular items.
            hits = await asyncio.to_thread(lambda: catalog.get_popular_items(limit=RESULTS_PER_PAGE))
            await self._show(interaction, self, SearchResultsView(self, hits, "Popular items"))
        elif category is None or (category in self.present and not self.whole_category):
            await self._show(interaction, self, self.fresh(page=0, category=category))
        else:
            # Not in these results: open that category from the whole catalog.
            hits = await asyncio.to_thread(lambda: catalog.search_items("", limit=CATEGORY_BROWSE_LIMIT, category=category))
            if not hits:
                await self._show(interaction, self, self.fresh(), f"Nothing found in **{category}** right now.")
                return
            view = SearchResultsView(self, hits, category, category=category, whole_category=True)
            await self._show(interaction, self, view)

    async def _series_picked(self, interaction: discord.Interaction) -> None:
        label, keyword, _emoji = self.series[int((interaction.data or {}).get("values", ["0"])[0])]
        hits = await asyncio.to_thread(lambda: ACNHCatalog.get_instance().search_items(keyword, limit=SEARCH_LIMIT))
        if not hits:
            await self._show(interaction, self, self.fresh(), f"Nothing found in **{label}** right now.")
            return
        await self._show(interaction, self, SearchResultsView(self, hits, f"{label} series"))

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary, row=3)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._show(interaction, self, self.fresh(page=self.page - 1))

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary, row=3)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._show(interaction, self, self.fresh(page=self.page + 1))

    @discord.ui.button(label="Search by name", emoji="🔍", style=discord.ButtonStyle.primary, row=3)
    async def new_search(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(SearchModal(self))

    @discord.ui.button(label="Type: All", emoji="🏷️", style=discord.ButtonStyle.secondary, row=3)
    async def toggle_kind(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        """Cycles All → Items only → DIY recipes only."""
        following = {"all": "items", "items": "diy", "diy": "all"}[self.kind]
        await self._show(interaction, self, self.fresh(page=0, kind=following))

    @discord.ui.button(label="Place order", emoji="✈️", style=discord.ButtonStyle.success, row=4)
    async def place_order(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        """Order straight from the item list, without going back to the order screen first (via the review)."""
        await self.cog.review_order(interaction, self)

    @discord.ui.button(label="Filters", emoji="🎛️", style=discord.ButtonStyle.secondary, row=3)
    async def open_filters(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        """Category, colour, season and language menu (shared with the DropBot catalog)."""
        view = CatalogFilterView(self.user_id, self.all_hits, self.filters, self.language, self._filters_done)
        await _swap(interaction, self, view.embed(), view)

    async def _filters_done(self, interaction: discord.Interaction, filters: CatalogFilters, language: str) -> None:
        view = SearchResultsView(
            self, self.all_hits, self.title,
            category=filters.category, color=filters.color, season=filters.season,
            whole_category=self.whole_category, kind=self.kind, recent=self.recent,
        )
        await _swap(interaction, None, view.embed(), view)


class MoreView(_SubView):
    """Rarely used tools, kept off the main order screen: saved loadouts, custom hex, clear."""

    def __init__(self, parent: "PocketView | _SubView"):
        super().__init__(parent)
        self._confirming = False
        self.clear.disabled = self.cog.cart(self.user_id).is_empty

    def embed(self) -> discord.Embed:
        embed = _embed("⚙️ More options")
        embed.add_field(name="💾 Saved loadouts", value="Save this order to reuse later, or load one you saved.", inline=False)
        embed.add_field(name="⌨️ Custom hex", value="Paste a full `!order` command from chopaeng.com or raw item codes.", inline=False)
        embed.add_field(name="🗑️ Clear order", value="Remove everything and start over.", inline=False)
        return embed

    @discord.ui.button(label="Saved loadouts", emoji="💾", style=discord.ButtonStyle.secondary, row=0)
    async def saved(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        loadouts = await asyncio.to_thread(_load_loadouts, self.user_id)
        await _swap(interaction, self, _loadouts_embed(loadouts), LoadoutView(self, loadouts))

    @discord.ui.button(label="Custom hex", emoji="⌨️", style=discord.ButtonStyle.secondary, row=0)
    async def custom_hex(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(CustomHexModal(self))

    @discord.ui.button(label="Clear order", emoji="🗑️", style=discord.ButtonStyle.danger, row=0)
    async def clear(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        """Two taps, like Cancel order; the order screen then offers Undo."""
        if not self._confirming:
            self._confirming = True
            button.label = "Tap again to clear everything"
            await interaction.response.edit_message(view=self)
            return
        cart = self.cog.cart(self.user_id)
        before = cart.to_json()
        cart.lines.clear()
        cart.villager = None
        self.cog.save_cart(self.user_id)
        await self.back_to_pocket(interaction, "Order cleared.", undo=before)


class ItemEditView(_SubView):
    """
    One item, like DropBot's item detail screen: picture, quantity, colour/pattern and a custom hex.

      • edit mode  – an item already in the order (`index`); the fifth button removes it
      • draft mode – an item picked from the results and not added yet (`draft`); the fifth button
        adds it to the order and returns to the results
    """

    def __init__(
        self,
        parent: "PocketView | _SubView",
        index: int | None = None,
        *,
        draft: OrderLine | None = None,
        results: "SearchResultsView | None" = None,
    ):
        super().__init__(parent)
        self.index = index
        self.draft = draft
        self.results = results
        line = self.line
        qty, cap = (line.qty if line else 1), self._max_qty()
        self.minus.disabled = self.one.disabled = qty <= 1
        self.plus.disabled = self.max_qty.disabled = qty >= cap
        self.reset_hex.disabled = not (line and line.override)
        if draft is not None:
            self.action.label, self.action.emoji = "Add to order", "✅"
            self.action.style = discord.ButtonStyle.success
        if results is None:
            self.remove_item(self.back_to_results)

        # Colour / pattern selects for items that come in several looks.
        self.variants = self._load_variants()
        self.current = next((v for v in self.variants if line and v.item.to_hex().upper() == line.hex), None)
        self.bodies = list(dict.fromkeys(v.body for v in self.variants))[:25]
        if len(self.bodies) > 1:
            color = discord.ui.Select(
                placeholder=f"🎨 Color: {self.current.body if self.current else 'Pick a color'}"[:100],
                options=[
                    discord.SelectOption(label=b[:100], value=str(i), default=bool(self.current and self.current.body == b))
                    for i, b in enumerate(self.bodies)
                ],
                row=1,
            )
            color.callback = self._color_picked   # type: ignore[method-assign]
            self.add_item(color)
        body = self.current.body if self.current else (self.bodies[0] if len(self.bodies) == 1 else None)
        self.patterns = [v for v in self.variants if v.body == body][:25] if body else []
        if len(self.patterns) > 1:
            pattern = discord.ui.Select(
                placeholder=f"🖌️ Pattern: {(self.current.pattern or 'Default') if self.current else 'Pick a pattern'}"[:100],
                options=[
                    discord.SelectOption(label=(v.pattern or "Default")[:100], value=str(i), default=v is self.current)
                    for i, v in enumerate(self.patterns)
                ],
                row=2,
            )
            pattern.callback = self._pattern_picked   # type: ignore[method-assign]
            self.add_item(pattern)

    # ── state ──
    @property
    def line(self) -> OrderLine | None:
        if self.draft is not None:
            return self.draft
        lines = self.cog.cart(self.user_id).lines
        return lines[self.index] if self.index is not None and 0 <= self.index < len(lines) else None

    def _catalog_item(self) -> CatalogItem | None:
        """The catalog record behind this line; None for raw hex codes."""
        line = self.line
        return _line_item(line) if line is not None else None

    def _load_variants(self) -> list[_Variant]:
        """Every look of this line's item; [] for raw hex codes and items with a single look."""
        item = self._catalog_item()
        return _item_variants(item) if item else []

    def _max_qty(self) -> int:
        """A stackable item tops out at one stack; anything else can use every free pocket slot."""
        line = self.line
        if line is None:
            return 1
        if line.stack > 1:
            return line.stack
        free = ORDER_MAX_SLOTS - self.cog.cart(self.user_id).slots
        return max(1, free if self.draft is not None else line.qty + free)

    def _again(self) -> "ItemEditView":
        return ItemEditView(self, self.index, draft=self.draft, results=self.results)

    async def _rerender(self, interaction: discord.Interaction, notice: str | None = None) -> None:
        if self.draft is None:
            self.cog.save_cart(self.user_id)
        view = self._again()
        await _swap(interaction, self, view.embed(notice), view)

    def embed(self, notice: str | None = None) -> discord.Embed:
        line, cart = self.line, self.cog.cart(self.user_id)
        if line is None:
            return _embed("✏️ Edit item", "That item is no longer in your order.")
        if self.draft is not None:
            hint = "Set the quantity" + (", pick a color or pattern" if self.variants else "") + ", then press **Add to order**."
        else:
            hint = "Change the quantity" + (", color or pattern" if self.variants else "") + ", or remove the item."
        embed = _embed(f"{'📦' if self.draft is not None else '✏️'} {line.label}", hint)
        embed.add_field(name="Quantity", value=f"**{line.qty}** (max {self._max_qty()})", inline=True)
        embed.add_field(name="Uses", value=f"{line.slots} slot{'s' if line.slots != 1 else ''}", inline=True)
        embed.add_field(name="🛒 Order", value=f"**{cart.slots}** / {ORDER_MAX_SLOTS} slots", inline=True)
        if line.override:
            embed.add_field(name="Hex", value=f"`{line.override}` (custom — **Reset hex** restores the catalog code)", inline=False)
        elif line.hex:
            embed.add_field(name="Hex", value=f"`{line.hex}`", inline=False)
        item = self._catalog_item()
        image = (self.current.item.image_url if self.current else "") or (item.image_url if item else "")
        if image:
            embed.set_thumbnail(url=image)
        if notice:
            embed.add_field(name="ℹ️", value=notice[:1000], inline=False)
        return embed

    # ── changes ──
    async def _set(self, interaction: discord.Interaction, qty: int) -> None:
        line = self.line
        if line is None:
            await self.back_to_pocket(interaction, "That item is no longer in your order.")
            return
        line.qty = max(1, min(qty, self._max_qty()))
        await self._rerender(interaction)

    async def _apply_variant(self, interaction: discord.Interaction, variant: _Variant) -> None:
        line, cart = self.line, self.cog.cart(self.user_id)
        if line is None:
            await self.back_to_pocket(interaction, "That item is no longer in your order.")
            return
        # A look is sent as its own hex, one slot each, so keep the quantity only if it still fits.
        qty = line.qty if line.stack == 1 else 1
        used = cart.slots if self.draft is not None else cart.slots - line.slots
        if used + qty > ORDER_MAX_SLOTS:
            qty = 1
        chosen = line_from_catalog(variant.item, qty)
        if self.draft is not None:
            self.draft = chosen
        else:
            cart.lines[self.index] = chosen   # type: ignore[index]
        await self._rerender(interaction)

    async def _color_picked(self, interaction: discord.Interaction) -> None:
        body = self.bodies[int((interaction.data or {}).get("values", ["0"])[0])]
        matching = [v for v in self.variants if v.body == body]
        # Keep the current pattern when the new color has it too, else that color's first pattern.
        keep = next((v for v in matching if self.current and v.pattern == self.current.pattern), matching[0])
        await self._apply_variant(interaction, keep)

    async def _pattern_picked(self, interaction: discord.Interaction) -> None:
        await self._apply_variant(interaction, self.patterns[int((interaction.data or {}).get("values", ["0"])[0])])

    async def set_override(self, interaction: discord.Interaction, code: str) -> None:
        """Set (or with "" clear) the hand-typed hex that is sent instead of the catalog code."""
        line = self.line
        if line is None:
            await self.back_to_pocket(interaction, "That item is no longer in your order.")
            return
        code = re.sub(r"^0x", "", code, flags=re.IGNORECASE).upper()
        if code and not _HEX_RE.fullmatch(code):
            await self._rerender(interaction, f"`{code[:20]}` isn't a hex code. Use 4 or 16 hex digits, like `0B64`.")
            return
        line.override = code
        await self._rerender(interaction, f"This item will be sent as `{code}`." if code else "Back to the catalog code.")

    # ── buttons ──
    @discord.ui.button(label="−", style=discord.ButtonStyle.secondary, row=0)
    async def minus(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._set(interaction, (self.line.qty if self.line else 1) - 1)

    @discord.ui.button(label="+", style=discord.ButtonStyle.secondary, row=0)
    async def plus(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._set(interaction, (self.line.qty if self.line else 1) + 1)

    @discord.ui.button(label="×1", style=discord.ButtonStyle.secondary, row=0)
    async def one(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._set(interaction, 1)

    @discord.ui.button(label="Max", style=discord.ButtonStyle.secondary, row=0)
    async def max_qty(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._set(interaction, self._max_qty())

    @discord.ui.button(label="Remove", emoji="🗑️", style=discord.ButtonStyle.danger, row=0)
    async def action(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        """Draft mode: add to the order. Edit mode: remove from the order."""
        line = self.line
        if self.draft is not None and self.results is not None:
            await self.results.add_line_and_stay(interaction, self, self.draft)
            return
        before = None
        if line is not None and self.index is not None:
            cart = self.cog.cart(self.user_id)
            before = cart.to_json()
            cart.lines.pop(self.index)
            self.cog.save_cart(self.user_id)
        await self.back_to_pocket(interaction, f"Removed **{line.label}**." if line else None, undo=before)

    async def set_amount(self, interaction: discord.Interaction, raw: str) -> None:
        """The "Amount…" box: any number, capped like the buttons."""
        text = raw.strip().lstrip("x×").strip()
        if not text.isdigit() or int(text) < 1:
            await self._rerender(interaction, f"`{raw.strip()[:12]}` isn't an amount. Type a whole number from 1 to {self._max_qty()}.")
            return
        wanted = int(text)
        await self._set(interaction, wanted)

    @discord.ui.button(label="Amount…", emoji="🔢", style=discord.ButtonStyle.secondary, row=3)
    async def amount(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(ItemAmountModal(self))

    @discord.ui.button(label="Custom hex", emoji="#️⃣", style=discord.ButtonStyle.secondary, row=3)
    async def custom_hex(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(ItemHexModal(self))

    @discord.ui.button(label="Reset hex", emoji="↩️", style=discord.ButtonStyle.secondary, row=3)
    async def reset_hex(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.set_override(interaction, "")

    @discord.ui.button(label="Back to results", emoji="🔍", style=discord.ButtonStyle.secondary, row=4)
    async def back_to_results(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        view = self.results.fresh()   # type: ignore[union-attr]
        await _swap(interaction, self, view.embed(), view)


def _bundle_items(bundle: dict) -> list[Any]:
    return bundle.get("orderItems") or bundle.get("items") or []


def _bundles_embed(bundles: list[dict]) -> discord.Embed:
    rows = []
    for bundle in bundles[:25]:
        name = bundle.get("title") or bundle.get("name") or "Bundle"
        desc = str(bundle.get("description") or "")[:90]
        rows.append(f"🎁 **{name}** ({len(_bundle_items(bundle))} items)" + (f"\n-# {desc}" if desc else ""))
    return _embed("🎁 Official bundles", "\n".join(rows)[:4000] or "No bundles.")


class BundleView(_SubView):
    def __init__(self, parent: PocketView, bundles: list[dict]):
        super().__init__(parent)
        self.bundles = bundles[:25]
        options = [
            discord.SelectOption(
                label=str(b.get("title") or b.get("name") or f"Bundle {i + 1}")[:100],
                value=str(i),
                description=str(b.get("category") or "")[:100] or None,
                emoji="🎁",
            )
            for i, b in enumerate(self.bundles)
        ]
        select = discord.ui.Select(placeholder="Add a bundle to your pocket…", options=options, row=0)
        select.callback = self._picked   # type: ignore[method-assign]
        self.add_item(select)

    async def _picked(self, interaction: discord.Interaction) -> None:
        bundle = self.bundles[int((interaction.data or {}).get("values", ["0"])[0])]
        notice = await self.cog.add_bundle(self.user_id, bundle)
        await self.back_to_pocket(interaction, notice)


def _loadouts_embed(loadouts: list[tuple[str, OrderCart]]) -> discord.Embed:
    if not loadouts:
        return _embed("💾 Saved loadouts", "Nothing saved yet. Fill your pocket, then press **Save current pocket**.")
    rows = [f"💾 **{discord.utils.escape_markdown(name)}** — {cart.slots} slots\n-# {cart_summary(cart, 90)}" for name, cart in loadouts]
    return _embed(f"💾 Saved loadouts ({len(loadouts)}/{MAX_LOADOUTS})", "\n".join(rows)[:4000])


class LoadoutView(_SubView):
    def __init__(self, parent: PocketView, loadouts: list[tuple[str, OrderCart]]):
        super().__init__(parent)
        self.loadouts = loadouts[:25]
        self.save_current.disabled = self.cog.cart(self.user_id).is_empty
        if self.loadouts:
            for placeholder, handler, emoji, row in (
                ("Load into pocket…", self._load, "💾", 0),
                ("Delete a loadout…", self._delete, "🗑️", 1),
            ):
                select = discord.ui.Select(
                    placeholder=placeholder,
                    options=[discord.SelectOption(label=name[:100], value=str(i), emoji=emoji) for i, (name, _) in enumerate(self.loadouts)],
                    row=row,
                )
                select.callback = handler   # type: ignore[method-assign]
                self.add_item(select)

    def _chosen(self, interaction: discord.Interaction) -> tuple[str, OrderCart]:
        return self.loadouts[int((interaction.data or {}).get("values", ["0"])[0])]

    async def _load(self, interaction: discord.Interaction) -> None:
        name, saved = self._chosen(interaction)
        self.cog.replace_cart(self.user_id, OrderCart.from_json(saved.to_json()))
        await self.back_to_pocket(interaction, f"Loaded **{name}**.")

    async def _delete(self, interaction: discord.Interaction) -> None:
        name, _ = self._chosen(interaction)
        await asyncio.to_thread(_delete_loadout, self.user_id, name)
        loadouts = await asyncio.to_thread(_load_loadouts, self.user_id)
        parent = PocketView(self.cog, self.user_id, self.username)
        parent.stop()
        await _swap(interaction, self, _loadouts_embed(loadouts), LoadoutView(parent, loadouts))

    @discord.ui.button(label="Save current pocket", emoji="💾", style=discord.ButtonStyle.primary, row=4)
    async def save_current(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(SaveLoadoutModal(self))


class SaveLoadoutModal(discord.ui.Modal, title="Save this pocket"):
    name = discord.ui.TextInput(label="Loadout name", placeholder="Starter materials", max_length=40)

    def __init__(self, parent: LoadoutView):
        super().__init__()
        self.parent = parent

    async def on_submit(self, interaction: discord.Interaction) -> None:
        view, name = self.parent, self.name.value.strip()
        existing = await asyncio.to_thread(_load_loadouts, view.user_id)
        if not name:
            notice = "Give the loadout a name."
        elif len(existing) >= MAX_LOADOUTS and name not in {n for n, _ in existing}:
            notice = f"You can keep {MAX_LOADOUTS} loadouts. Delete one first."
        else:
            await asyncio.to_thread(_save_loadout, view.user_id, name, view.cog.cart(view.user_id))
            notice = f"Saved **{name}**."
        await view.back_to_pocket(interaction, notice)


class HistoryView(_SubView):
    """Past orders with one-click reorder (loads the old order into the pocket)."""

    def __init__(self, parent: PocketView, orders: list[dict], labels: dict[str, str] | None = None):
        super().__init__(parent)
        self.orders = [o for o in orders if o.get("command")][:25]
        labels = labels or {}
        if self.orders:
            options = [
                discord.SelectOption(
                    label=_history_label(o, labels, 100),
                    value=str(i),
                    description=f"{o.get('status', '')} · {time.strftime('%b %d', time.gmtime(int(o.get('created_at') or 0)))}"[:100],
                    emoji="🔁",
                )
                for i, o in enumerate(self.orders)
            ]
            select = discord.ui.Select(placeholder="Reorder a past pocket…", options=options, row=0)
            select.callback = self._reorder   # type: ignore[method-assign]
            self.add_item(select)

    async def _reorder(self, interaction: discord.Interaction) -> None:
        order = self.orders[int((interaction.data or {}).get("values", ["0"])[0])]
        cart = await asyncio.to_thread(_order_cart, str(order.get("id") or ""))
        if cart.is_empty:
            cart = await asyncio.to_thread(_cart_from_command, str(order["command"]))
        if cart.is_empty:
            await self.back_to_pocket(interaction, "I couldn't rebuild that order. Add the items again.")
            return
        self.cog.replace_cart(self.user_id, cart)
        await self.back_to_pocket(interaction, "Loaded your past order. Review it, then place the order.")


def _history_label(order: dict, labels: dict[str, str], limit: int) -> str:
    """What an order contained, in words (falls back to its stored command)."""
    text = labels.get(str(order.get("id"))) or readable_command(str(order.get("command") or ""), limit)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def build_history_embed(orders: list[dict], labels: dict[str, str] | None = None) -> discord.Embed:
    if not orders:
        return _embed("🕘 Order history", "No orders yet. Press **Start Order** to place your first one!")
    icons = {"completed": "✅", "cancelled": "🚫", "error": "⚠️", "ready": "✈️", "preparing": "🛠️", "queued": "⏳"}
    labels = labels or {}
    rows = []
    for order in orders[:10]:
        what = discord.utils.escape_markdown(_history_label(order, labels, 120))
        rows.append(f"{icons.get(str(order.get('status')), '•')} <t:{int(order.get('created_at') or 0)}:R> — {what}")
    embed = _embed("🕘 Order history", "\n".join(rows)[:4000])
    if any(o.get("command") for o in orders):
        embed.description += "\n\n-# Pick one in the menu to load it into your pocket again."
    return embed


class ReviewView(_SubView):
    """The last screen before an order is sent: the full pocket, the line, and a Send button."""

    def __init__(self, parent: "PocketView | _SubView", accepting: bool):
        super().__init__(parent)
        self.send.disabled = not accepting or self.cog.cart(self.user_id).is_empty
        self.back.label = "Change something"
        self.back.emoji = "✏️"

    @discord.ui.button(label="Send order", emoji="✈️", style=discord.ButtonStyle.success, row=0)
    async def send(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()
        self.stop()
        # On failure submit_order shows the order screen with the reason.
        await self.cog.submit_order(interaction, PocketView(self.cog, self.user_id, self.username))


# ─── Views: order tracker ─────────────────────────────────────────────────────

class OrderTrackerView(discord.ui.View):
    def __init__(self, cog: "OrderBotCog", user_id: int, order_id: str, summary: str):
        super().__init__(timeout=TOKEN_SECONDS)
        self.cog = cog
        self.user_id = user_id
        self.order_id = order_id
        self.summary = summary
        self._confirming = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This order isn't yours.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Refresh", emoji="🔄", style=discord.ButtonStyle.secondary)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()
        self.stop()
        self.cog.start_tracker(interaction.user, self.order_id, interaction, self.summary)

    @discord.ui.button(label="Cancel order", emoji="✖️", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self._confirming:
            self._confirming = True
            button.label = "Tap again to cancel"
            await interaction.response.edit_message(view=self)
            return
        await interaction.response.defer()
        self.stop()
        await self.cog.cancel_order(interaction, self.order_id, self.summary)


class ReorderView(discord.ui.View):
    """Shown when an order ends without a pickup: one tap sends the same pocket again."""

    def __init__(self, cog: "OrderBotCog", user_id: int, order_id: str):
        super().__init__(timeout=TOKEN_SECONDS)
        self.cog = cog
        self.user_id = user_id
        self.order_id = order_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This order isn't yours.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Reorder same pocket", emoji="🔁", style=discord.ButtonStyle.success)
    async def reorder(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        username = await _order_gate(interaction)
        if not username:
            return
        await interaction.response.defer()
        cart = await asyncio.to_thread(_order_cart, self.order_id)
        if cart.is_empty:
            await interaction.followup.send(
                embed=_err_embed("I couldn't rebuild that order.", hint="Press **Start Order** and add the items again."),
                ephemeral=True,
            )
            return
        self.stop()
        self.cog.replace_cart(self.user_id, cart)
        # On failure submit_order shows the pocket (with the reason) in place of this message.
        await self.cog.submit_order(interaction, PocketView(self.cog, self.user_id, username))


# ─── Views: persistent panel ──────────────────────────────────────────────────

class OrderPanelView(discord.ui.View):
    """
    Static custom_ids, so bot.add_view() in cog_load keeps every panel working after a restart.
    "Notify me" is only shown while the Order Bot is closed.
    """

    def __init__(self, cog: "OrderBotCog", is_open: bool = True):
        super().__init__(timeout=None)
        self.cog = cog
        if is_open:
            self.remove_item(self.notify)
        self.add_item(discord.ui.Button(label="Web Order Bot", emoji="🌐", style=discord.ButtonStyle.link, url=WEB_ORDER_URL, row=1))

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        logger.error("[OrderBot] Panel button error: %s", error, exc_info=error)
        await _reply(interaction, "❌ Something went wrong. Please try again in a moment.")

    @discord.ui.button(label="Start Order", emoji="🛒", style=discord.ButtonStyle.success, custom_id="orderbot:start", row=0)
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.open_pocket(interaction)

    @discord.ui.button(label="My Order", emoji="📦", style=discord.ButtonStyle.primary, custom_id="orderbot:myorder", row=0)
    async def my_order(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.show_my_order(interaction)

    @discord.ui.button(label="Queue", emoji="📋", style=discord.ButtonStyle.secondary, custom_id="orderbot:queue", row=0)
    async def queue(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.show_queue(interaction)

    @discord.ui.button(label="History", emoji="🕘", style=discord.ButtonStyle.secondary, custom_id="orderbot:history", row=0)
    async def history(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.show_history(interaction)

    @discord.ui.button(label="Guide", emoji="📖", style=discord.ButtonStyle.secondary, custom_id="orderbot:guide", row=1)
    async def guide(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_message(embed=build_guide_embed(), ephemeral=True)

    @discord.ui.button(label="Notify me when open", emoji="🔔", style=discord.ButtonStyle.primary, custom_id="orderbot:notify", row=1)
    async def notify(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.subscribe_open_ping(interaction)


# ─── Cog ──────────────────────────────────────────────────────────────────────

class OrderBotCog(commands.Cog, name="OrderBot"):
    """Order Bot panel, pocket builder and live order tracking. Needs no privileged intents."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._carts: dict[int, OrderCart] = {}
        self._trackers: dict[int, asyncio.Task] = {}       # user_id -> running tracker
        self._panel_sigs: dict[int, str] = {}              # channel_id -> last rendered panel
        self._dead_panels: set[int] = set()
        self._panel_views: dict[bool, OrderPanelView] = {}  # is_open -> view
        self._panel_messages: dict[int, int] = {}          # channel_id -> panel message id (sticky)
        self._last_panel: tuple[discord.Embed, str, bool] | None = None
        self._channel_locks: dict[int, asyncio.Lock] = {}  # serialises refresh vs. sticky repost
        self._bump_tasks: dict[int, asyncio.Task] = {}
        self._bump_first_seen: dict[int, float] = {}
        self._is_open: bool | None = None                  # last known "taking orders" state
        self._open_streak = 0                              # consecutive refreshes the bot was open
        self._notify_pending = True                        # subscribers may be waiting (unknown at startup)
        self._next_notified: set[str] = set()              # order ids that got the "almost your turn" DM
        self._status_line: str | None = None               # "🟢 Open · 4 in line" for the order screen
        self._status_at = 0.0                              # monotonic time _status_line was built
        self._now_serving: str | None = None               # who the Order Bot is serving, from the last queue read

    def _panel_view(self, is_open: bool) -> OrderPanelView:
        view = self._panel_views.get(is_open)
        if view is None:
            view = self._panel_views[is_open] = OrderPanelView(self, is_open)
        return view

    async def cog_load(self) -> None:
        await asyncio.to_thread(_init_tables)
        # The "closed" view carries every button, so registering it covers both layouts.
        self.bot.add_view(self._panel_view(True))
        self.bot.add_view(self._panel_view(False))
        self.refresh_panels_loop.start()
        _spawn(self._resume_trackers())

    async def cog_unload(self) -> None:
        self.refresh_panels_loop.cancel()
        for task in list(self._trackers.values()) + list(self._bump_tasks.values()):
            task.cancel()
        for view in self._panel_views.values():
            view.stop()

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            await _reply(interaction, embed=_err_embed("You need the **Manage Messages** permission to use this command."))
            return
        logger.error("[OrderBot] Command error: %s", error, exc_info=error)
        await _reply(interaction, embed=_err_embed("Something went wrong running that command."))

    # ── Pocket state ──────────────────────────────────────────────────────────

    def cart(self, user_id: int) -> OrderCart:
        """The member's pocket. Call load_cart() first in a flow so a saved pocket is restored."""
        return self._carts.setdefault(user_id, OrderCart())

    async def load_cart(self, user_id: int) -> OrderCart:
        if user_id not in self._carts:
            self._carts[user_id] = await asyncio.to_thread(_load_cart, user_id)
            await asyncio.to_thread(cf.get_user_language, user_id)   # cached for the catalog views
        return self._carts[user_id]

    def pocket_embed(self, user_id: int, username: str, notice: str | None = None) -> discord.Embed:
        """The order screen with the latest known Order Bot status (open? how long is the line?)."""
        fresh = self._status_line if time.monotonic() - self._status_at < STATUS_LINE_MAX_AGE * 3 else None
        return build_pocket_embed(self.cart(user_id), username, notice, fresh)

    async def refresh_status_line(self) -> None:
        """Re-read the bot status and queue when the cached line is old. Never raises."""
        if self._status_line and time.monotonic() - self._status_at < STATUS_LINE_MAX_AGE:
            return
        try:
            await self._panel_state()
        except Exception as exc:
            logger.debug("[OrderBot] Could not refresh the status line: %s", exc)

    def save_cart(self, user_id: int) -> None:
        _spawn(asyncio.to_thread(_save_cart, user_id, self.cart(user_id)))

    def replace_cart(self, user_id: int, cart: OrderCart) -> None:
        self._carts[user_id] = cart
        self.save_cart(user_id)

    async def add_text(self, user_id: int, raw: str) -> str | None:
        """Add typed items (and an optional villager:<id>) to the pocket. Returns a notice for the member."""
        lines, villager, problems = await asyncio.to_thread(parse_order_text, raw)
        return await self._merge(user_id, lines, villager, problems)

    async def add_hex(self, user_id: int, raw: str) -> str | None:
        """Add raw hex codes (the "Custom hex" button). Anything that isn't a hex code is rejected."""
        lines, villager, problems = parse_hex_command(raw)
        return await self._merge(user_id, lines, villager, problems)

    async def _merge(self, user_id: int, lines: list[OrderLine], villager: str | None, problems: list[str]) -> str | None:
        cart = self.cart(user_id)
        added = 0
        for line in lines:
            error = cart.add(line)
            if error:
                problems.append(error)
            else:
                added += 1
        if villager:
            note = await self.set_villager(user_id, villager)
            if note:
                problems.append(note)
        self.save_cart(user_id)
        notice = ([f"Added {added} item{'s' if added != 1 else ''}."] if added else []) + problems
        return "\n".join(notice)[:1000] or None

    async def set_villager(self, user_id: int, query: str) -> str:
        found = await asyncio.to_thread(lambda: ACNHCatalog.get_instance().get_villager(query.strip()))
        if not found:
            return f"I couldn't find a villager called **{query.strip()[:40]}**."
        self.cart(user_id).villager = (found.name, found.villager_id)
        self.save_cart(user_id)
        return f"🏠 **{found.name}** will be in boxes when you arrive. Have an empty plot ready."

    async def add_bundle(self, user_id: int, bundle: dict) -> str:
        def _lines() -> list[OrderLine]:
            out: list[OrderLine] = []
            for entry in _bundle_items(bundle):
                if isinstance(entry, dict):
                    name = str(entry.get("name") or "").strip()
                    qty = max(1, min(int(entry.get("quantity") or entry.get("count") or 1), ORDER_MAX_SLOTS))
                    line = (_resolve_entry(name, qty) if name else None) or (
                        _resolve_entry(str(entry.get("itemId") or entry.get("id") or ""), qty)
                    )
                    if line:
                        out.append(line)
                elif isinstance(entry, str):
                    out.extend(parse_order_text(entry)[0])
            return out

        cart = self.cart(user_id)
        lines = await asyncio.to_thread(_lines)
        problems = [e for e in (cart.add(line) for line in lines) if e]
        self.save_cart(user_id)
        name = bundle.get("title") or bundle.get("name") or "bundle"
        added = len(lines) - len(problems)
        return "\n".join([f"Added {added} item{'s' if added != 1 else ''} from **{name}**."] + problems[:3])

    async def bundles(self) -> list[dict]:
        try:
            data = await _client().get_bundles()
        except Exception as exc:
            logger.warning("[OrderBot] Could not load bundles: %s", exc)
            return []
        return [b for b in data if isinstance(b, dict) and _bundle_items(b)]

    # ── Panel flows ───────────────────────────────────────────────────────────

    async def find_active_order(self, user_id: int) -> tuple[str, dict, str] | None:
        """(order_id, live status, summary) if the member has an order in progress."""
        row = await asyncio.to_thread(_active_order_row, user_id)
        if not row:
            return None
        order_id = str(row["id"])
        summary, _ = await asyncio.to_thread(_discord_order_info, order_id)
        data = await _client().get_order_status(order_id)
        if _is_unreachable(data):
            # Can't confirm either way; trust the DB so a member can't double-order during an outage.
            return order_id, {"status": row["status"]}, summary or row["command"][:120]
        if data.get("status") in ACTIVE_STATUSES:
            return order_id, data, summary or row["command"][:120]
        return None

    async def open_pocket(self, interaction: discord.Interaction, prefill: str | None = None, villager: str | None = None) -> None:
        """Start Order: the pocket builder, or the tracker when an order is already in progress."""
        username = await _order_gate(interaction)
        if not username:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        user = interaction.user
        active = await self.find_active_order(user.id)
        if active:
            order_id, _, summary = active
            self.start_tracker(user, order_id, interaction, summary)
            return
        await self.load_cart(user.id)
        notes = []
        if prefill:
            notes.append(await self.add_text(user.id, prefill))
        if villager:
            notes.append(await self.set_villager(user.id, villager))
        notice = "\n".join(n for n in notes if n) or None
        await self.refresh_status_line()
        view = PocketView(self, user.id, username)
        embed = self.pocket_embed(user.id, username, notice)
        await interaction.followup.send(embed=embed, view=_adopt(view, interaction), ephemeral=True)

    async def review_order(self, interaction: discord.Interaction, parent: "PocketView | _SubView") -> None:
        """Place order → the review screen with the full pocket, the line, and Send order."""
        await interaction.response.defer()
        cart = self.cart(parent.user_id)
        if cart.is_empty:
            await _show_pocket(parent, interaction, "❌ Your pocket is empty.")
            return
        client = _client()
        status, queue_data = await asyncio.gather(client.get_bot_status(), client.get_queue())
        view = ReviewView(parent, _bot_accepting(status))
        await _swap(interaction, parent, build_review_embed(cart, parent.username, status, queue_data), view)

    async def show_my_order(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        active = await self.find_active_order(interaction.user.id)
        if not active:
            await interaction.followup.send(
                embed=_embed("📦 No active order", "You don't have an order in progress. Press **Start Order** to place one."),
                ephemeral=True,
            )
            return
        order_id, _, summary = active
        self.start_tracker(interaction.user, order_id, interaction, summary)

    async def show_queue(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        data = await _client().get_queue()
        await interaction.followup.send(embed=build_queue_embed(data, interaction.user), ephemeral=True)

    async def show_history(self, interaction: discord.Interaction) -> None:
        username = await _order_gate(interaction)
        if not username:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        orders = await _client().get_user_order_history(str(interaction.user.id), limit=10)
        labels = await asyncio.to_thread(history_labels, orders)
        await self.load_cart(interaction.user.id)
        parent = PocketView(self, interaction.user.id, username)
        parent.stop()   # only used to carry the cog/user/username into the sub-menu
        view = HistoryView(parent, orders, labels)
        await interaction.followup.send(embed=build_history_embed(orders, labels), view=_adopt(view, interaction), ephemeral=True)

    # ── Submit / cancel ───────────────────────────────────────────────────────

    async def submit_order(self, interaction: discord.Interaction, view: PocketView) -> None:
        """Send the pocket to SysBot. `interaction` is already deferred; all output edits its message."""
        user = interaction.user
        cart = self.cart(user.id)

        async def fail(message: str) -> None:
            await view.render(interaction, f"❌ {message}")

        if cart.is_empty:
            await fail("Your pocket is empty.")
            return
        wait = _cooldown_left(user.id, "orderbot", "submit", SUBMIT_COOLDOWN)
        if wait > 0:
            await fail(f"Please wait {wait:.0f}s before placing another order.")
            return

        active = await self.find_active_order(user.id)
        if active:
            order_id, _, summary = active
            view.stop()
            self.start_tracker(user, order_id, interaction, summary)
            return

        client = _client()
        status = await client.get_bot_status()
        if not _bot_accepting(status):
            _pop_cooldown(user.id, "orderbot", "submit")
            await fail("The Order Bot isn't taking orders right now. Your pocket is saved — try again in a bit.")
            return

        try:
            order_text = await asyncio.to_thread(build_order_text, cart.lines)
        except ValueError as exc:
            _pop_cooldown(user.id, "orderbot", "submit")
            await fail(str(exc))
            return

        result = await client.submit_order(
            username=view.username,
            order_text=order_text,
            villager=cart.villager[1] if cart.villager else None,
            user_id=str(user.id),
        )
        order_id = str(result.get("order_id") or "") if isinstance(result, dict) else ""
        if not order_id or result.get("success") is False:
            _pop_cooldown(user.id, "orderbot", "submit")
            error = str((result or {}).get("error") or (result or {}).get("message") or "The Order Bot rejected the order.")
            logger.warning("[OrderBot] Order by %s rejected: %s", user.id, error)
            await fail(error[:300])
            return

        summary = cart_summary(cart, 300)
        logger.info("[OrderBot] Order %s placed by %s (%s): %s", order_id, user, user.id, order_text[:200])
        await asyncio.to_thread(_record_discord_order, order_id, user.id, summary, cart.to_json())
        self.replace_cart(user.id, OrderCart())
        view.stop()
        self.start_tracker(user, order_id, interaction, summary, initial=result)

    async def cancel_order(self, interaction: discord.Interaction, order_id: str, summary: str) -> None:
        task = self._trackers.pop(interaction.user.id, None)
        if task:
            task.cancel()
        result = await _client().cancel_order(order_id)
        embed = build_order_embed({"status": "cancelled"}, order_id, summary)
        if isinstance(result, dict) and result.get("success") is False and result.get("error"):
            embed.add_field(
                name="⚠️ Heads up",
                value="I couldn't confirm the cancellation with the Order Bot. If it still calls you, just don't fly in.",
                inline=False,
            )
        logger.info("[OrderBot] Order %s cancelled by %s", order_id, interaction.user.id)
        with contextlib.suppress(discord.HTTPException):
            await interaction.edit_original_response(embed=embed, view=None)

    # ── Tracking ──────────────────────────────────────────────────────────────

    def start_tracker(
        self,
        user: discord.abc.User,
        order_id: str,
        interaction: discord.Interaction | None,
        summary: str = "",
        *,
        initial: dict | None = None,
    ) -> None:
        """Follow an order until it ends. Replaces any tracker the member already has."""
        old = self._trackers.pop(user.id, None)
        if old:
            old.cancel()
        self._trackers[user.id] = _spawn(self._track(user, order_id, interaction, summary, initial))

    async def _track(
        self,
        user: discord.abc.User,
        order_id: str,
        interaction: discord.Interaction | None,
        summary: str,
        initial: dict | None,
    ) -> None:
        client = _client()
        started = time.monotonic()
        view = OrderTrackerView(self, user.id, order_id, summary) if interaction else None
        _, dm_sent = await asyncio.to_thread(_discord_order_info, order_id)
        next_sent = order_id in self._next_notified or await asyncio.to_thread(_next_sent, order_id)
        seen_far = False   # was further back than NEXT_UP_POSITION, so "almost your turn" is news
        last_sig: tuple | None = None
        last_status = ""
        misses = 0
        start_position = 0          # furthest back in line we've seen; the progress bar counts from here
        ready_at: float | None = None   # when the gate opened (SysBot doesn't say when it closes)
        data = initial

        async def show(embed: discord.Embed, *, final: bool = False, final_view: discord.ui.View | None = None) -> None:
            if interaction is None or time.monotonic() - started > TOKEN_SECONDS:
                return
            with contextlib.suppress(discord.HTTPException):
                await interaction.edit_original_response(embed=embed, view=final_view if final else view)

        try:
            while time.monotonic() - started < TRACK_MAX_SECONDS:
                if data is None:
                    data = await client.get_order_status(order_id)
                if _is_unreachable(data):
                    misses += 1
                    # Show it right away if nothing is on screen yet, otherwise only once it persists.
                    if (misses == 1 and last_sig is None) or misses == 3:
                        await show(build_order_embed({"status": last_status or "queued"}, order_id, summary, connection_lost=True))
                    data = None
                    await asyncio.sleep(POLL_SECONDS * 2)
                    continue
                misses = 0

                status = str(data.get("status") or "queued")
                # SysBot forgets an order once the visitor has left; after "ready" that means it was picked up.
                if status == "error" and last_status == "ready":
                    status, data = "completed", {**data, "status": "completed", "message": ""}
                last_status = status

                position_now = data.get("queue_position")
                if status == "queued" and isinstance(position_now, int):
                    start_position = max(start_position, position_now)
                data = {**data, "start_position": start_position, "now_serving": self._now_serving if status == "queued" else None}
                if status == "ready" and data.get("dodo_code"):
                    ready_at = ready_at or time.time()
                    if not data.get("gate_closes_at"):
                        data["gate_closes_at"] = int(ready_at + _gate_seconds())

                sig = (
                    status, data.get("queue_position"), data.get("estimated_minutes"), data.get("dodo_code"),
                    data.get("message"), data.get("now_serving"),
                )
                ended_early = status in ("cancelled", "error")   # no pickup: offer the same pocket again
                if sig != last_sig:
                    last_sig = sig
                    await show(
                        build_order_embed(data, order_id, summary),
                        final=status in TERMINAL_STATUSES,
                        final_view=ReorderView(self, user.id, order_id) if ended_early else None,
                    )

                # "Almost your turn" DM, only for members who actually waited further back in line.
                position = data.get("queue_position") if status == "queued" else 0
                if status == "queued" and isinstance(position, int) and position > NEXT_UP_POSITION:
                    seen_far = True
                elif seen_far and not next_sent and status in ("queued", "preparing"):
                    next_sent = True
                    self._next_notified.add(order_id)
                    await asyncio.to_thread(_mark_next_sent, order_id)
                    await self._send_next_up(user, data, order_id)

                if status == "ready" and data.get("dodo_code") and not dm_sent:
                    dm_sent = True
                    await asyncio.to_thread(_mark_dm_sent, order_id)
                    await self._deliver_dodo(user, data, order_id, summary, interaction)

                if status in TERMINAL_STATUSES:
                    # They were sent a Dodo but the gate closed: the tracker may be long gone, so DM it too.
                    if ended_early and dm_sent and _missed_gate(data):
                        await self._send_missed_gate(user, data, order_id, summary)
                    self._next_notified.discard(order_id)
                    return
                data = None
                await asyncio.sleep(POLL_SECONDS_READY if status == "ready" else POLL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[OrderBot] Tracker for order %s failed: %s", order_id, exc, exc_info=exc)
        finally:
            if view is not None:
                view.stop()
            if self._trackers.get(user.id) is asyncio.current_task():
                self._trackers.pop(user.id, None)

    async def _deliver_dodo(
        self,
        user: discord.abc.User,
        data: dict,
        order_id: str,
        summary: str,
        interaction: discord.Interaction | None,
    ) -> None:
        """DM the Dodo code. If DMs are closed, nudge the member in the order channel (without the code)."""
        try:
            await user.send(embed=build_order_embed(data, order_id, summary))
            logger.info("[OrderBot] Sent Dodo for order %s to %s by DM", order_id, user.id)
            return
        except discord.HTTPException as exc:
            logger.info("[OrderBot] Could not DM %s for order %s: %s", user.id, order_id, exc)

        channel_id = interaction.channel_id if interaction else getattr(Config, "ORDER_BOT_CHANNEL_ID", None)
        channel = self.bot.get_channel(channel_id) if channel_id else None
        if channel is not None and hasattr(channel, "send"):
            with contextlib.suppress(discord.HTTPException):
                await channel.send(
                    f"✈️ {user.mention} your order is ready, but your DMs are closed. "
                    "Press **My Order** on the Order Bot panel to see your Dodo code.",
                    delete_after=300,
                    allowed_mentions=discord.AllowedMentions(users=[user]),
                )

    async def _send_next_up(self, user: discord.abc.User, data: dict, order_id: str) -> None:
        position = data.get("queue_position")
        where = f"You're **#{position}** in line" if data.get("status") == "queued" and position else "You're up next"
        embed = _embed(
            "🛫 Almost your turn!",
            f"{where} for **{data.get('island_name') or _order_island()}**.\n"
            "Head to your airport now — your Dodo code arrives here in a moment and the gate only waits a short time.",
        )
        embed.set_footer(text=f"{PANEL_FOOTER_TEXT} • Order {order_id}", icon_url=PANEL_FOOTER_ICON)
        with contextlib.suppress(discord.HTTPException):
            await user.send(embed=embed)

    async def _send_missed_gate(self, user: discord.abc.User, data: dict, order_id: str, summary: str) -> None:
        embed = build_order_embed(data, order_id, summary)
        embed.add_field(
            name="🔁 Want it again?",
            value="Press **Start Order → History** on the Order Bot panel (or use `/myorders`) to reorder the same pocket.",
            inline=False,
        )
        with contextlib.suppress(discord.HTTPException):
            await user.send(embed=embed)

    async def _resume_trackers(self) -> None:
        """After a restart, keep following Discord-placed orders so their Dodo DM still goes out."""
        await self.bot.wait_until_ready()
        for order_id, user_id, summary in await asyncio.to_thread(_resumable_orders):
            if user_id in self._trackers:
                continue
            try:
                user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            except discord.HTTPException:
                continue
            self.start_tracker(user, order_id, None, summary)
            logger.info("[OrderBot] Resumed tracking order %s for %s", order_id, user_id)

    # ── Panel refresh ─────────────────────────────────────────────────────────

    async def _panel_state(self) -> tuple[discord.Embed, str, bool]:
        """(embed, change signature, taking orders) from the live bot status and queue."""
        client = _client()
        status, queue_data = await asyncio.gather(client.get_bot_status(), client.get_queue())
        embed = build_panel_embed(status, queue_data)
        is_open = _bot_accepting(status)
        self._status_line, self._status_at = status_line(status, queue_data), time.monotonic()
        serving = queue_data.get("current_active_user") if isinstance(queue_data, dict) else None
        self._now_serving = str(serving) if serving else None
        sig = json.dumps([is_open, embed.description, [(f.name, f.value) for f in embed.fields]])
        self._last_panel = (embed, sig, is_open)
        return embed, sig, is_open

    def _lock_for(self, channel_id: int) -> asyncio.Lock:
        lock = self._channel_locks.get(channel_id)
        if lock is None:
            lock = self._channel_locks[channel_id] = asyncio.Lock()
        return lock

    @tasks.loop(seconds=PANEL_REFRESH_SECONDS)
    async def refresh_panels_loop(self) -> None:
        panels = [p for p in await asyncio.to_thread(_load_panels) if p[0] not in self._dead_panels]
        # In memory is authoritative once known: a sticky repost may be newer than the row just read.
        live = {channel_id for channel_id, _ in panels}
        for channel_id, message_id in panels:
            self._panel_messages.setdefault(channel_id, message_id)
        for channel_id in [c for c in self._panel_messages if c not in live]:
            del self._panel_messages[channel_id]
        if not panels:
            return
        try:
            embed, sig, is_open = await self._panel_state()
        except Exception as exc:
            logger.warning("[OrderBot] Could not build the panel: %s", exc)
            return

        # Two open refreshes in a row before pinging, so a one-off blip doesn't spend everyone's alert.
        self._is_open = is_open
        self._open_streak = self._open_streak + 1 if is_open else 0
        if self._open_streak >= 2 and self._notify_pending:
            self._notify_pending = False
            _spawn(self._send_open_pings(panels[0][0]))

        for channel_id in live:
            if self._panel_sigs.get(channel_id) == sig:
                continue
            channel = self.bot.get_channel(channel_id)
            if channel is None or not hasattr(channel, "get_partial_message"):
                continue
            async with self._lock_for(channel_id):
                message_id = self._panel_messages.get(channel_id)
                if not message_id:
                    continue
                try:
                    await channel.get_partial_message(message_id).edit(embed=embed, view=self._panel_view(is_open))
                    self._panel_sigs[channel_id] = sig
                except discord.NotFound:
                    # Deleted or lost: adopt one found in recent history, else post a new one.
                    new_id = await self._recover_panel(channel, embed, is_open)
                    if new_id:
                        self._panel_messages[channel_id] = new_id
                        self._panel_sigs[channel_id] = sig
                        await asyncio.to_thread(_save_panel, channel_id, new_id)
                        logger.info("[OrderBot] Panel in %s was missing; restored as message %s.", channel_id, new_id)
                    else:
                        self._dead_panels.add(channel_id)
                        self._panel_messages.pop(channel_id, None)
                        logger.warning("[OrderBot] Panel in %s is gone and could not be restored; run /orderpanel.", channel_id)
                except discord.HTTPException as exc:
                    logger.warning("[OrderBot] Could not refresh the panel in %s: %s", channel_id, exc)

    async def _recover_panel(self, channel: Any, embed: discord.Embed, is_open: bool) -> int | None:
        """The panel message no longer exists. Returns the id of the panel now in the channel, or None."""
        view = self._panel_view(is_open)
        bot_id = getattr(self.bot.user, "id", None)
        if hasattr(channel, "history"):
            try:
                async for candidate in channel.history(limit=25):
                    if (
                        candidate.author.id == bot_id and candidate.components and candidate.embeds
                        and (candidate.embeds[0].title or "").startswith(PANEL_TITLE_PREFIX)
                    ):
                        await candidate.edit(embed=embed, view=view)
                        return candidate.id
            except discord.HTTPException as exc:
                logger.debug("[OrderBot] Could not scan %s for an existing panel: %s", channel.id, exc)
        try:
            return (await channel.send(embed=embed, view=view)).id
        except discord.HTTPException as exc:
            logger.warning("[OrderBot] Could not repost the panel in %s: %s", channel.id, exc)
            return None

    @refresh_panels_loop.before_loop
    async def _before_refresh(self) -> None:
        await self.bot.wait_until_ready()

    # ── Sticky panel ──────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Any new message in a panel channel buries the panel: queue a repost to the bottom."""
        channel_id = message.channel.id
        known = self._panel_messages.get(channel_id)
        if not known or message.id == known:
            return
        if (
            self.bot.user and message.author.id == self.bot.user.id and message.components and message.embeds
            and (message.embeds[0].title or "").startswith(PANEL_TITLE_PREFIX)
        ):
            return   # our own freshly posted panel
        self._schedule_bump(channel_id)

    def _schedule_bump(self, channel_id: int) -> None:
        """Debounce reposts: wait for STICKY_DELAY_SECONDS of quiet, capped at STICKY_MAX_WAIT_SECONDS."""
        now = time.monotonic()
        pending = self._bump_tasks.get(channel_id)
        if pending and not pending.done():
            first = self._bump_first_seen.get(channel_id, now)
            if now - first >= STICKY_MAX_WAIT_SECONDS - STICKY_DELAY_SECONDS:
                return   # busy channel: let the pending repost fire instead of postponing it again
            pending.cancel()
        else:
            self._bump_first_seen[channel_id] = now
        self._bump_tasks[channel_id] = asyncio.create_task(self._bump_after_delay(channel_id))

    async def _bump_after_delay(self, channel_id: int) -> None:
        try:
            await asyncio.sleep(STICKY_DELAY_SECONDS)
        except asyncio.CancelledError:
            return
        try:
            await self._bump_panel(channel_id)
        except Exception as exc:
            logger.warning("[OrderBot] Sticky repost failed in channel %s: %s", channel_id, exc, exc_info=exc)
        finally:
            if self._bump_tasks.get(channel_id) is asyncio.current_task():
                self._bump_tasks.pop(channel_id, None)
                self._bump_first_seen.pop(channel_id, None)

    async def _bump_panel(self, channel_id: int) -> None:
        """Repost the panel as the newest message, then delete the old one."""
        async with self._lock_for(channel_id):
            old_id = self._panel_messages.get(channel_id)
            channel = self.bot.get_channel(channel_id)
            if not old_id or channel is None or not hasattr(channel, "history"):
                return
            async for last in channel.history(limit=1):
                if last.id == old_id:
                    return   # already at the bottom
            embed, sig, is_open = self._last_panel or await self._panel_state()
            new_msg = await channel.send(embed=embed, view=self._panel_view(is_open))
            self._panel_messages[channel_id] = new_msg.id
            self._panel_sigs[channel_id] = sig
            await asyncio.to_thread(_save_panel, channel_id, new_msg.id)
            with contextlib.suppress(discord.HTTPException):
                await channel.get_partial_message(old_id).delete()

    # ── "Open again" pings ────────────────────────────────────────────────────

    async def subscribe_open_ping(self, interaction: discord.Interaction) -> None:
        if self._is_open:
            await interaction.response.send_message("🟢 The Order Bot is open right now — press **Start Order**.", ephemeral=True)
            return
        await asyncio.to_thread(_add_subscriber, interaction.user.id)
        self._notify_pending = True
        await interaction.response.send_message(
            "🔔 Got it — I'll DM you once when the Order Bot is taking orders again. Keep your DMs open.",
            ephemeral=True,
        )

    async def _send_open_pings(self, channel_id: int) -> None:
        user_ids = await asyncio.to_thread(_pop_subscribers)
        if not user_ids:
            return
        embed = _embed(
            "🟢 The Order Bot is open again!",
            f"**{_order_island()}** is taking orders. Head to <#{channel_id}> and press **Start Order**.\n"
            "-# You asked for this one-time alert with the 🔔 button.",
        )
        sent = 0
        for user_id in user_ids[:NOTIFY_MAX]:
            try:
                user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
                await user.send(embed=embed)
                sent += 1
            except discord.HTTPException:
                pass
            await asyncio.sleep(1)   # stay well under the DM rate limit
        logger.info("[OrderBot] Sent %d/%d 'open again' pings", sent, len(user_ids))

    # ── Autocomplete ──────────────────────────────────────────────────────────

    async def _items_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        """Completes the item being typed after the last comma, keeping what came before it."""
        head, _, tail = (current or "").rpartition(",")
        prefix = f"{head.strip()}, " if head.strip() else ""
        query = re.sub(r"\s*[x×]?\s*\d+$", "", tail.strip(), flags=re.IGNORECASE).strip()
        try:
            catalog = ACNHCatalog.get_instance()
            if query:
                hits = await asyncio.to_thread(lambda: catalog.search_items(query, limit=20))
            else:
                hits = catalog.get_popular_items(limit=20)
        except Exception as exc:
            logger.debug("[OrderBot] Item autocomplete failed: %s", exc)
            return []
        choices: list[app_commands.Choice[str]] = []
        seen: set[str] = set()
        for item in hits:
            stack = _stack_size(item)
            value = f"{prefix}{item.name} {stack}" if stack > 1 else f"{prefix}{item.name}"
            if value in seen or len(value) > 100:
                continue
            seen.add(value)
            choices.append(app_commands.Choice(name=value, value=value))
        return choices[:25]

    async def _villager_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        try:
            catalog = ACNHCatalog.get_instance()
            query = (current or "").strip()
            if query:
                hits = await asyncio.to_thread(lambda: catalog.search_villagers(query, limit=25))
            else:
                hits = catalog.get_popular_villagers(limit=25)
        except Exception as exc:
            logger.debug("[OrderBot] Villager autocomplete failed: %s", exc)
            return []
        return [app_commands.Choice(name=f"{v.emoji} {v.display_name}"[:100], value=v.name[:100]) for v in hits[:25]]

    # ── Slash commands ────────────────────────────────────────────────────────

    @app_commands.command(name="order", description="Order items from the Order Bot island and get a private Dodo code")
    @app_commands.describe(
        items="Items, comma separated (e.g. Gold nugget 30, Royal crown). Leave empty to open your pocket.",
        villager="A villager to move in with your order (optional)",
    )
    @app_commands.autocomplete(items=_items_autocomplete, villager=_villager_autocomplete)
    @app_commands.guild_only()
    async def slash_order(self, interaction: discord.Interaction, items: str | None = None, villager: str | None = None) -> None:
        await self.open_pocket(interaction, prefill=items, villager=villager)

    @app_commands.command(name="cart", description="Open your Order Bot pocket")
    @app_commands.guild_only()
    async def slash_cart(self, interaction: discord.Interaction) -> None:
        await self.open_pocket(interaction)

    @app_commands.command(name="myorders", description="Track your active order and reorder past ones")
    @app_commands.guild_only()
    async def slash_myorders(self, interaction: discord.Interaction) -> None:
        if await asyncio.to_thread(_active_order_row, interaction.user.id):
            await self.show_my_order(interaction)
        else:
            await self.show_history(interaction)

    @app_commands.command(name="queue", description="See who is in line for the Order Bot")
    @app_commands.guild_only()
    async def slash_queue(self, interaction: discord.Interaction) -> None:
        await self.show_queue(interaction)

    @app_commands.command(name="presets", description="Browse official Order Bot bundles")
    @app_commands.guild_only()
    async def slash_presets(self, interaction: discord.Interaction) -> None:
        username = await _order_gate(interaction)
        if not username:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        bundles = await self.bundles()
        if not bundles:
            await interaction.followup.send(embed=_embed("🎁 Official bundles", "No bundles are available right now."), ephemeral=True)
            return
        await self.load_cart(interaction.user.id)
        parent = PocketView(self, interaction.user.id, username)
        parent.stop()
        view = BundleView(parent, bundles)
        await interaction.followup.send(embed=_bundles_embed(bundles), view=_adopt(view, interaction), ephemeral=True)

    @app_commands.command(name="orderpanel", description="Post the Order Bot panel in this channel")
    @app_commands.describe(remove="Remove the panel from this channel instead of posting it")
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.guild_only()
    async def slash_orderpanel(self, interaction: discord.Interaction, remove: bool = False) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send("❌ The panel can only be posted in a regular text channel.", ephemeral=True)
            return
        if remove:
            # The panel repairs itself when deleted, so removing it has to be explicit.
            async with self._lock_for(channel.id):
                old_ids = {m for c, m in await asyncio.to_thread(_load_panels) if c == channel.id}
                old_ids.add(self._panel_messages.pop(channel.id, None))
                await asyncio.to_thread(_delete_panel, channel.id)
                self._panel_sigs.pop(channel.id, None)
                for old_id in old_ids:
                    if old_id:
                        with contextlib.suppress(discord.HTTPException):
                            await channel.get_partial_message(old_id).delete()
            logger.info("[OrderBot] Panel removed from %s by %s", channel.id, interaction.user)
            await interaction.followup.send(f"✅ Order Bot panel removed from <#{channel.id}>.", ephemeral=True)
            return
        embed, sig, is_open = await self._panel_state()
        async with self._lock_for(channel.id):
            old_ids = {m for c, m in await asyncio.to_thread(_load_panels) if c == channel.id}
            old_ids.add(self._panel_messages.get(channel.id))
            message = await channel.send(embed=embed, view=self._panel_view(is_open))
            self._panel_messages[channel.id] = message.id
            self._panel_sigs[channel.id] = sig
            self._dead_panels.discard(channel.id)
            await asyncio.to_thread(_save_panel, channel.id, message.id)
            for old_id in old_ids:
                if old_id and old_id != message.id:
                    with contextlib.suppress(discord.HTTPException):
                        await channel.get_partial_message(old_id).delete()
        logger.info("[OrderBot] Panel posted in %s (msg %s) by %s", channel.id, message.id, interaction.user)
        await interaction.followup.send(f"✅ Order Bot panel posted in <#{channel.id}>.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(OrderBotCog(bot))
