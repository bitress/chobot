"""Drop-input parsing, villager name matching and catalog search."""

from __future__ import annotations

import difflib
import json
import logging
import os
import re
from utils.acnh_catalog import ACNHCatalog, CatalogItem, CatalogVillager

from .constants import CATALOG_ITEM_LIMIT, MAX_DROP_SLOTS, MAX_ITEM_NAME, MAX_STACK

logger = logging.getLogger("DropBotCog")


# ─── Drop input parsing ────────────────────────────────────────────────────────

_ITEM_NAME_RE = re.compile(rf"^[A-Za-z0-9][A-Za-z0-9 '\-\.\(\)&]{{0,{MAX_ITEM_NAME - 1}}}$")


_ENTRY_RE = re.compile(r"^(?P<item>.+?)(?:\s*[x×]\s*(?P<qx>[0-9]+)|\s+(?P<q>[0-9]+))?$", re.IGNORECASE)


def parse_drop_items(raw: str) -> tuple[list[tuple[str, int]] | None, str | None]:
    """
    Parse "Gold nugget 30, 09C9 x10, Royal Crown" into [(item, qty), ...].
    Returns (entries, None) or (None, user-facing error).
    Item names are checked for length and characters only; SysBot remains the authority on names/hex.
    """
    entries = [e.strip() for e in re.split(r"[,\n;]+", raw or "") if e.strip()]
    if not entries:
        return None, "Enter at least one item."
    if len(entries) > MAX_DROP_SLOTS:
        return None, (
            f"You can drop at most **{MAX_DROP_SLOTS}** different items at once "
            f"(you entered {len(entries)})."
        )

    parsed: list[tuple[str, int]] = []
    for entry in entries:
        m = _ENTRY_RE.match(entry)
        if not m:
            return None, f"I couldn't read “{entry[:40]}”. Use `name quantity`, for example `Wood 30`."
        item = m.group("item").strip()
        qty = int(m.group("qx") or m.group("q") or 1)
        if not _ITEM_NAME_RE.match(item):
            return None, (
                f"“{item[:40]}” isn't a valid item name or code. "
                f"Use letters, numbers and spaces only (up to {MAX_ITEM_NAME} characters)."
            )
        if not 1 <= qty <= MAX_STACK:
            return None, f"Quantity for **{item}** must be between 1 and {MAX_STACK}."
        parsed.append((item, qty))
    return parsed, None


def build_drop_payload(entries: list[tuple[str, int]]) -> str:
    """Build a space-separated SysBot hex/item drop string with NO COMMAS."""
    if not entries:
        return ""
    try:
        from utils.data_manager import resolve_drop_items
        formatted_entries = [f"{item} {qty}" if qty > 1 else item for item, qty in entries]
        hex_str, _ = resolve_drop_items(formatted_entries)
        if hex_str:
            return hex_str
    except Exception:
        pass

    parts: list[str] = []
    for item, qty in entries:
        clean = item.strip()
        try:
            from utils.sysbot_api import sanitize_single_drop_item
            sanitized = sanitize_single_drop_item(clean)
            if sanitized:
                parts.extend([sanitized] * qty)
                continue
        except Exception:
            pass
        parts.extend([clean] * qty)
    return " ".join(parts)


def _format_item_for_sysbot(item_name: str, qty: int) -> str:
    """Format an item for SysBot/NHSE using hex codes.
    Repeats the hex code for quantity, space-separated with NO commas.
    """
    return build_drop_payload([(item_name, qty)])


def _normalize_items(entries: list[tuple[str, int]]) -> str:
    """Normalize drop items into space-separated hex codes with NO COMMAS."""
    return build_drop_payload(entries)


# ─── Villager matching ─────────────────────────────────────────────────────────

_VILLAGER_NAMES: list[str] = []


_VILLAGER_NAMES_LOADED = False


def _get_villager_names() -> list[str]:
    global _VILLAGER_NAMES, _VILLAGER_NAMES_LOADED
    if _VILLAGER_NAMES_LOADED:
        return _VILLAGER_NAMES
    try:
        # villagers.json is in the project root
        vpath = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "villagers.json",
        )
        with open(vpath, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            _VILLAGER_NAMES = sorted(
                (v.get("name") or v.get("Name") or k.title())
                for k, v in data.items()
                if isinstance(v, dict)
            ) or sorted(k.title() for k in data)
        elif isinstance(data, list):
            _VILLAGER_NAMES = sorted(
                v.get("name") or v.get("Name") or ""
                for v in data
                if isinstance(v, dict)
            )
        _VILLAGER_NAMES = [n for n in _VILLAGER_NAMES if n]
        _VILLAGER_NAMES_LOADED = True
    except Exception as exc:
        logger.warning("[DropBot] Could not load villagers.json: %s", exc)
        _VILLAGER_NAMES = []
    return _VILLAGER_NAMES


def _match_villagers(query: str, limit: int = 5) -> tuple[str | None, list[str]]:
    """
    Returns (exact, candidates).
      exact      – the villager name if `query` matches one case-insensitively, else None
      candidates – up to `limit` plausible names (prefix, substring, then close spellings)
    """
    q = (query or "").strip()
    if not q:
        return None, []

    # ACNHCatalog is the primary source (all official villagers)
    try:
        catalog = ACNHCatalog.get_instance()
        exact_v = catalog.get_villager(q)
        if exact_v:
            return exact_v.name, []

        hits = catalog.search_villagers(q, limit=limit)
        if hits:
            return None, [h.name for h in hits]
    except Exception as exc:
        logger.debug("[DropBot] ACNHCatalog search in _match_villagers failed: %s", exc)

    # Fallback to local villagers.json
    names = _get_villager_names()
    if not names:
        return None, []

    lower_map = {n.lower(): n for n in names}
    if q.lower() in lower_map:
        return lower_map[q.lower()], []

    pool: list[str] = [n for n in names if n.lower().startswith(q.lower())]
    pool += [n for n in names if q.lower() in n.lower() and n not in pool]
    for close in difflib.get_close_matches(q.lower(), list(lower_map), n=limit, cutoff=0.6):
        name = lower_map[close]
        if name not in pool:
            pool.append(name)
    return None, pool[:limit]


def _search_catalog(query: str) -> tuple[list[CatalogItem], list[CatalogVillager]]:
    """Blocking catalog search; run in a thread."""
    catalog = ACNHCatalog.get_instance()
    return (
        catalog.search_items(query, limit=CATALOG_ITEM_LIMIT),
        catalog.search_villagers(query, limit=10),
    )
