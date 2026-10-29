"""The drop cart: persistent cart model, summary, and item colour/pattern variants."""

from __future__ import annotations

import asyncio
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any
from utils.acnh_catalog import ACNHCatalog, CatalogItem

from .constants import CART_SAVE_DELAY, MAX_DROP_SLOTS, MAX_STACK
from .state import _spawn
from .db import _save_cart_rows


# ─── Catalog search (drop cart, category filter, persistent across searches) ───

@dataclass
class _CartEntry:
    item: CatalogItem
    qty: int
    hex_override: str | None = None   # hand-typed hex; wins over the catalog hex when dropping
    _cart: Any = field(default=None, init=False, repr=False, compare=False)   # owning _PersistentCart

    def __setattr__(self, name: str, value: Any) -> None:
        object.__setattr__(self, name, value)
        cart = self.__dict__.get("_cart")
        if cart is not None and name != "_cart":
            cart.touch()   # qty / hex edits happen in place, so entries report their own changes

    @property
    def drop_hex(self) -> str:
        return self.hex_override or self.item.to_hex() or self.item.internal_id or self.item.name


class _PersistentCart(dict):
    """
    The drop cart: a dict of key -> _CartEntry that saves itself (debounced) per user and island.
    Any mutation, including in-place edits to an entry, schedules a save. Without a user, an island
    or a running event loop it behaves as a plain dict.
    """

    def __init__(self, user_id: int = 0, island: str | None = None):
        super().__init__()
        self.user_id = user_id
        self.island = island
        self._save_pending = False

    # -- persistence --
    def snapshot(self) -> list[dict[str, Any]]:
        return [
            {"key": k, "name": e.item.name, "qty": e.qty, "hex": e.drop_hex, "override": e.hex_override}
            for k, e in self.items()
        ]

    def touch(self) -> None:
        if not (self.user_id and self.island) or self._save_pending:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._save_pending = True
        _spawn(self._save_later())

    async def _save_later(self) -> None:
        await asyncio.sleep(CART_SAVE_DELAY)
        self._save_pending = False
        await asyncio.to_thread(_save_cart_rows, self.user_id, self.island or "", self.snapshot())

    def adopt(self, key: str, entry: _CartEntry) -> None:
        """Insert without scheduling a save (used when restoring a saved cart)."""
        entry._cart = self
        dict.__setitem__(self, key, entry)

    # -- mutations --
    def __setitem__(self, key: str, entry: _CartEntry) -> None:
        entry._cart = self
        super().__setitem__(key, entry)
        self.touch()

    def __delitem__(self, key: str) -> None:
        super().__delitem__(key)
        self.touch()

    def pop(self, key: str, *default: Any) -> Any:
        res = super().pop(key, *default)
        self.touch()
        return res

    def popitem(self) -> tuple[str, _CartEntry]:
        res = super().popitem()
        self.touch()
        return res

    def clear(self) -> None:
        super().clear()
        self.touch()

    def setdefault(self, key: str, default: Any = None) -> Any:
        if key not in self:
            self[key] = default
        return self[key]

    def update(self, *args: Any, **kwargs: Any) -> None:
        for k, v in dict(*args, **kwargs).items():
            self[k] = v


def _restore_cart_entries(rows: list[dict[str, Any]]) -> list[tuple[str, _CartEntry]]:
    """Blocking (catalog lookups). Rebuild cart entries from saved rows, best match first."""
    catalog = ACNHCatalog.get_instance()
    restored: list[tuple[str, _CartEntry]] = []
    for row in rows[:MAX_DROP_SLOTS]:
        try:
            name = str(row.get("name") or "")
            key = str(row.get("key") or "")
            qty = max(1, min(int(row.get("qty") or 1), MAX_STACK))
            drop_hex = str(row.get("hex") or "")
            override = row.get("override") or None
        except (TypeError, ValueError):
            continue
        if not name:
            continue
        item = next((c for c in catalog.search_items(name, limit=100) if _cart_key(c) == key), None)
        if item is None:
            item = catalog.get_item(name)
            # Variant no longer resolvable by key: keep what the user chose by pinning its hex.
            if item is not None and drop_hex and drop_hex != (item.to_hex() or item.internal_id or item.name):
                override = override or (drop_hex if len(drop_hex) in (4, 16) else None)
        if item is None:
            if len(drop_hex) not in (4, 16):
                continue
            item = CatalogItem(name=name, category="Restored", internal_id=drop_hex)
            override = override or drop_hex
        restored.append((_cart_key(item), _CartEntry(item, qty, override)))
    return restored


def _stack_size(item: CatalogItem) -> int:
    try:
        return max(1, int(item.stack_size or 1))
    except (TypeError, ValueError):
        return 1


POCKET_SLOTS = 40   # in-game inventory size, for the "will this fit" hint


def _cart_summary(cart: dict[str, _CartEntry]) -> tuple[str, list[str]]:
    """(one-line totals, warnings) describing a cart before it is dropped."""
    if not cart:
        return "", []
    total = sum(e.qty for e in cart.values())
    stacks = sum(math.ceil(e.qty / _stack_size(e.item)) for e in cart.values())
    totals = f"📊 **{total}** item{'s' if total != 1 else ''} · ~**{stacks}** stack{'s' if stacks != 1 else ''}"

    warnings: list[str] = []
    if len(cart) >= MAX_DROP_SLOTS:
        warnings.append(f"⚠️ Cart is full ({MAX_DROP_SLOTS}/{MAX_DROP_SLOTS} slots) — drop it before adding more.")
    if stacks > POCKET_SLOTS:
        warnings.append(
            f"⚠️ ~{stacks} stacks is more than a full {POCKET_SLOTS}-slot pocket — pick items up in batches."
        )
    by_name: Counter[str] = Counter(e.item.display_name for e in cart.values())
    for name, n in by_name.items():
        if n > 1:
            warnings.append(f"🔁 **{name}** is in the cart {n} times (different colors/hex) — intended?")
    by_hex: Counter[str] = Counter(e.drop_hex for e in cart.values())
    for hex_code, n in by_hex.items():
        if n > 1:
            warnings.append(f"🔁 Hex `{hex_code}` appears {n} times — those entries drop the same item.")
    return totals, warnings


def _default_qty(it: CatalogItem) -> int:
    try:
        return max(1, min(int(it.stack_size or 1), MAX_STACK))
    except (TypeError, ValueError):
        return 1


def _cart_key(it: CatalogItem) -> str:
    """Key cart entries by internal hex code or ID:variant to avoid name collisions."""
    hex_code = it.to_hex()
    if hex_code:
        return hex_code
    if it.internal_id or it.variant_id or it.variation:
        return f"{it.internal_id}:{it.variant_id or it.variation or ''}"
    return f"it_{abs(hash((it.name, it.category))):08x}"


# ─── Item colours / patterns (mirrors the chopaeng.com Command Builder) ───────

_PATTERN_VARIANT_RE = re.compile(r"^\d+_\d+$")


_HEX_INPUT_RE = re.compile(r"^(?:[0-9A-F]{4}|[0-9A-F]{16})$")


_NO_PATTERN_LABEL = "Default"


@dataclass
class _Variant:
    item: CatalogItem   # a CatalogItem for exactly this colour/pattern (its to_hex() is the drop hex)
    body: str           # colour name, e.g. "Silver"
    pattern: str        # pattern name, "" when the variant has none


def _clean_variant_text(value: Any) -> str:
    s = str(value or "").strip()
    return "" if s.upper() in ("NA", "NONE") else s


def _item_variants(it: CatalogItem) -> list[_Variant]:
    """
    Every colour/pattern of `it`, or [] if it only comes in one look.
    Hex rules match the web Command Builder (generateFullItemHex):
      • pattern variants ("body_pattern", e.g. "1_3") share one internal id and are encoded
        into a 16-char hex (0000<variant>0000<id>, fences use their own layout);
      • colours with their own internal id (clothing, accessories…) drop as that 4-char id.
    """
    if it.category == "Recipes":
        return []
    raw = it.raw_data or {}
    variations = raw.get("variations") or []
    if len(variations) < 2:
        return []

    base_name = raw.get("name") or it.name
    out: list[_Variant] = []
    for v in variations:
        if not isinstance(v, dict):
            continue
        raw_id = v.get("internalId", raw.get("internalId"))
        if raw_id is None:
            continue
        try:
            internal_id = f"{int(raw_id):04X}"
        except (TypeError, ValueError):
            internal_id = str(raw_id).strip().upper().zfill(4)

        body = _clean_variant_text(v.get("variation"))
        pattern = _clean_variant_text(v.get("pattern"))
        label = f"{body} / {pattern}" if body and pattern else (body or pattern)
        vid = str(v.get("variantId") or "").strip()

        out.append(_Variant(
            item=CatalogItem(
                name=base_name,
                category=it.category,
                internal_id=internal_id,
                image_url=v.get("image") or v.get("storageImage") or v.get("closetImage") or it.image_url,
                variation=label,
                variant_id=vid if _PATTERN_VARIANT_RE.match(vid) else "NA",
                stack_size=it.stack_size,
                diy=it.diy,
                raw_data=raw,
            ),
            body=body or _NO_PATTERN_LABEL,
            pattern=pattern,
        ))
    return out if len(out) > 1 else []


def _match_variant_index(it: CatalogItem, variants: list[_Variant]) -> int:
    """Index of the variant `it` currently represents (0 if it can't be told apart)."""
    hx = it.to_hex()
    for i, v in enumerate(variants):
        if v.item.to_hex() == hx:
            return i
    cur = (it.variation or "").strip().lower()
    for i, v in enumerate(variants):
        if v.item.internal_id == it.internal_id and v.item.variation.lower() == cur:
            return i
    for i, v in enumerate(variants):
        if v.item.internal_id == it.internal_id:
            return i
    return 0


def _unique(seq: list[str]) -> list[str]:
    return list(dict.fromkeys(seq))
