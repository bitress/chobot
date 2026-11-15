"""
Catalog filters and languages shared by the DropBot and OrderBot catalogs.

  • Colour filter   – an item matches when any of its variations has the colour
  • Season filter   – the four seasons plus the big events, read from the item's season/event tags
  • Languages       – item names in the 11 game languages, for display and for searching
  • Per-user language preference, stored in `user_catalog_prefs`

Everything here is synchronous and in-memory except the two preference helpers that touch the DB.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Iterable

from utils.acnh_catalog import ACNHCatalog, CatalogItem
from utils.database import connect_db

logger = logging.getLogger("CatalogFilters")


# ─── Colours ──────────────────────────────────────────────────────────────────

# (catalog colour, emoji) in a rainbow-ish order. Names match the `colors` lists in the catalog data.
COLORS: list[tuple[str, str]] = [
    ("Red", "🔴"),
    ("Orange", "🟠"),
    ("Yellow", "🟡"),
    ("Green", "🟢"),
    ("Aqua", "💧"),
    ("Blue", "🔵"),
    ("Purple", "🟣"),
    ("Pink", "🌸"),
    ("Brown", "🟤"),
    ("Beige", "🟨"),
    ("White", "⚪"),
    ("Gray", "🔘"),
    ("Black", "⚫"),
    ("Colorful", "🌈"),
]
COLOR_EMOJI = dict(COLORS)


# ─── Seasons and events ───────────────────────────────────────────────────────

# (label, emoji, keywords). A tag such as "Halloween (days before); Halloween" matches a group when
# it contains one of the keywords, so an item can be in several groups (Halloween is also Fall).
SEASONS: list[tuple[str, str, tuple[str, ...]]] = [
    ("Spring", "🌸", ("spring", "cherry-blossom", "bunny day", "shamrock", "nature day", "may day")),
    ("Summer", "☀️", ("summer", "fireworks", "wedding season")),
    ("Fall", "🍂", ("fall", "autumn", "acorns", "maple", "mushrooms", "halloween", "turkey day", "moon-viewing")),
    ("Winter", "❄️", ("winter", "snowflakes", "ornaments", "toy day", "countdown", "new year", "festivale", "shōgatsu")),
    ("Bunny Day", "🥚", ("bunny day",)),
    ("Wedding Season", "💍", ("wedding season",)),
    ("Fireworks Show", "🎆", ("fireworks",)),
    ("Halloween", "🎃", ("halloween",)),
    ("Turkey Day", "🦃", ("turkey day",)),
    ("Toy Day", "🎁", ("toy day", "ornaments")),
    ("New Year", "🎍", ("countdown", "new year", "shōgatsu")),
    ("Festivale", "🎭", ("festivale",)),
]
SEASON_EMOJI = {label: emoji for label, emoji, _ in SEASONS}

# Tournaments list every season in their tag ("Fishing Tourney (Spring); … (Winter)") but aren't seasonal.
_NOT_SEASONAL = ("fishing tourney", "bug-off")
_SEASON_KEYS = ("seasonEvent", "seasonalAvailability")


# ─── Languages ────────────────────────────────────────────────────────────────

DEFAULT_LANGUAGE = "uSen"

# (translation key in the catalog data, label, flag)
LANGUAGES: list[tuple[str, str, str]] = [
    ("uSen", "English", "🇺🇸"),
    ("eUes", "Español", "🇪🇸"),
    ("eUfr", "Français", "🇫🇷"),
    ("eUde", "Deutsch", "🇩🇪"),
    ("eUit", "Italiano", "🇮🇹"),
    ("eUnl", "Nederlands", "🇳🇱"),
    ("eUru", "Русский", "🇷🇺"),
    ("jPja", "日本語", "🇯🇵"),
    ("kRko", "한국어", "🇰🇷"),
    ("cNzh", "简体中文", "🇨🇳"),
    ("tWzh", "繁體中文", "🇹🇼"),
]
LANGUAGE_LABELS = {code: f"{flag} {label}" for code, label, flag in LANGUAGES}
_LANGUAGE_CODES = {code for code, _, _ in LANGUAGES}
# Regional duplicates that are searched too, so "uSfr"-only spellings are still found.
_SEARCH_ONLY_CODES = ("eUen", "uSfr", "uSes")


# ─── Per-item facts (cached) ──────────────────────────────────────────────────

# id(raw_data) -> (raw_data, colours, seasons). The record itself is kept so a recycled id (after a
# catalog reload, say) can never return another item's facts.
_FACTS: dict[int, tuple[dict, frozenset[str], frozenset[str]]] = {}


def _records(raw: dict) -> list[dict]:
    variations = [v for v in (raw.get("variations") or []) if isinstance(v, dict)]
    return [raw] + variations


def _season_groups(tags: Iterable[str]) -> frozenset[str]:
    found: set[str] = set()
    for tag in tags:
        text = str(tag or "").lower()
        if not text or any(skip in text for skip in _NOT_SEASONAL):
            continue
        for label, _emoji, keywords in SEASONS:
            if any(keyword in text for keyword in keywords):
                found.add(label)
    return frozenset(found)


def _facts(item: CatalogItem) -> tuple[frozenset[str], frozenset[str]]:
    raw = item.raw_data or {}
    if not raw:
        return frozenset(), frozenset()
    cached = _FACTS.get(id(raw))
    if cached is None or cached[0] is not raw:
        colors: set[str] = set()
        tags: list[str] = []
        for record in _records(raw):
            colors.update(str(c) for c in (record.get("colors") or []) if c)
            tags.extend(str(record.get(key) or "") for key in _SEASON_KEYS)
        cached = _FACTS[id(raw)] = (raw, frozenset(colors), _season_groups(tags))
    return cached[1], cached[2]


def item_colors(item: CatalogItem) -> frozenset[str]:
    """Every colour any variation of `item` has."""
    return _facts(item)[0]


def item_seasons(item: CatalogItem) -> frozenset[str]:
    """The season/event groups (labels from SEASONS) `item` belongs to."""
    return _facts(item)[1]


# ─── Filters ──────────────────────────────────────────────────────────────────

@dataclass
class CatalogFilters:
    category: str | None = None
    color: str | None = None
    season: str | None = None

    @property
    def active(self) -> bool:
        return bool(self.category or self.color or self.season)

    def matches(self, item: CatalogItem) -> bool:
        if self.category and item.category != self.category:
            return False
        if self.color and self.color not in item_colors(item):
            return False
        if self.season and self.season not in item_seasons(item):
            return False
        return True

    def apply(self, items: Iterable[CatalogItem]) -> list[CatalogItem]:
        return [item for item in items if self.matches(item)]

    def describe(self) -> str:
        """Short human-readable summary, "" when nothing is filtered."""
        parts = []
        if self.category:
            parts.append(f"🗂️ {self.category}")
        if self.color:
            parts.append(f"{COLOR_EMOJI.get(self.color, '🎨')} {self.color}")
        if self.season:
            parts.append(f"{SEASON_EMOJI.get(self.season, '📅')} {self.season}")
        return " · ".join(parts)


def available_colors(items: Iterable[CatalogItem]) -> list[str]:
    """Colours that occur in `items`, in display order."""
    present: set[str] = set()
    for item in items:
        present |= item_colors(item)
    return [color for color, _ in COLORS if color in present]


def available_seasons(items: Iterable[CatalogItem]) -> list[str]:
    """Season/event groups that occur in `items`, in display order."""
    present: set[str] = set()
    for item in items:
        present |= item_seasons(item)
    return [label for label, _, _ in SEASONS if label in present]


# ─── Translated names ─────────────────────────────────────────────────────────

def _translations(item: CatalogItem) -> dict[str, Any]:
    tr = (item.raw_data or {}).get("translations")
    return tr if isinstance(tr, dict) else {}


def localized_name(item: CatalogItem, language: str | None) -> str:
    """The item's name in `language`, with its variation in brackets like display_name; English as fallback."""
    if not language or language == DEFAULT_LANGUAGE:
        return item.display_name
    name = str(_translations(item).get(language) or "").strip()
    if not name:
        return item.display_name
    if item.variation and item.variation.lower() not in ("na", "none", ""):
        return f"{name} ({item.variation})"
    return name


def _fold(text: str) -> str:
    """Case-insensitive key that keeps non-Latin scripts intact (the catalog's own normaliser drops them)."""
    return " ".join(str(text or "").casefold().split())


_TRANSLATION_INDEX: list[tuple[str, str, CatalogItem]] | None = None   # (folded name, language, item)
_TRANSLATION_EXACT: dict[str, CatalogItem] | None = None
_TRANSLATION_SOURCE: Any = None   # the catalog item list the index was built from


def _translation_index() -> list[tuple[str, str, CatalogItem]]:
    global _TRANSLATION_INDEX, _TRANSLATION_EXACT, _TRANSLATION_SOURCE
    catalog = ACNHCatalog.get_instance()
    if _TRANSLATION_INDEX is None or _TRANSLATION_SOURCE is not catalog.items:   # rebuilt after a reload
        _TRANSLATION_SOURCE = catalog.items
        index: list[tuple[str, str, CatalogItem]] = []
        exact: dict[str, CatalogItem] = {}
        seen: set[int] = set()
        for item in list(catalog.items) + list(catalog.recipes):
            raw = item.raw_data or {}
            if not raw or id(raw) in seen:
                continue   # one entry per catalog record, not one per variation
            seen.add(id(raw))
            tr = _translations(item)
            for code in list(_LANGUAGE_CODES) + list(_SEARCH_ONLY_CODES):
                name = _fold(tr.get(code) or "")
                if name:
                    index.append((name, code, item))
                    exact.setdefault(name, item)
        _TRANSLATION_INDEX, _TRANSLATION_EXACT = index, exact
    return _TRANSLATION_INDEX


def find_by_translation(name: str) -> CatalogItem | None:
    """The item whose name in any game language is exactly `name` (case-insensitive)."""
    _translation_index()
    return (_TRANSLATION_EXACT or {}).get(_fold(name))


def search_translations(query: str, limit: int = 25, language: str | None = None) -> list[CatalogItem]:
    """
    Items whose translated name contains `query`: exact, then prefix, then substring. With `language`
    only that language is searched, otherwise all of them.
    """
    q = _fold(query)
    if len(q) < 2:
        return []
    tiers: tuple[list[CatalogItem], list[CatalogItem], list[CatalogItem]] = ([], [], [])
    for name, code, item in _translation_index():
        if language and code != language:
            continue
        if q not in name:
            continue
        tiers[0 if name == q else 1 if name.startswith(q) else 2].append(item)
    out: list[CatalogItem] = []
    seen: set[int] = set()
    for tier in tiers:
        for item in tier:
            if id(item) not in seen:
                seen.add(id(item))
                out.append(item)
                if len(out) >= limit:
                    return out
    return out


def search_items(query: str, limit: int = 25, language: str | None = None) -> list[CatalogItem]:
    """
    Catalog search that also understands item names in the other game languages.

    With a non-English `language`, names in that language rank first; the normal English search
    follows, then names in any other language. Blocking: run in a thread.
    """
    catalog = ACNHCatalog.get_instance()
    english = catalog.search_items(query, limit=limit)
    if language and language != DEFAULT_LANGUAGE:
        groups = [search_translations(query, limit, language), english, search_translations(query, limit)]
    else:
        groups = [english, search_translations(query, limit)]
    out: list[CatalogItem] = []
    seen: set[tuple[str, str, str]] = set()
    for group in groups:
        for item in group:
            key = (item.name, item.variation, item.category)
            if key not in seen:
                seen.add(key)
                out.append(item)
                if len(out) >= limit:
                    return out
    return out


# ─── Per-user language preference ─────────────────────────────────────────────

_USER_LANGUAGE: dict[int, str] = {}


def get_user_language(user_id: int) -> str:
    """The member's catalog language (cached). Blocking on the first call per user."""
    cached = _USER_LANGUAGE.get(user_id)
    if cached:
        return cached
    language = DEFAULT_LANGUAGE
    try:
        with connect_db() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS user_catalog_prefs "
                "(user_id VARCHAR(64) PRIMARY KEY, language VARCHAR(16) NOT NULL, updated_at INTEGER NOT NULL)"
            )
            row = conn.execute("SELECT language FROM user_catalog_prefs WHERE user_id = ?", (str(user_id),)).fetchone()
            if row and row["language"] in _LANGUAGE_CODES:
                language = row["language"]
    except Exception as exc:
        logger.debug("[CatalogFilters] Could not read the language for %s: %s", user_id, exc)
    _USER_LANGUAGE[user_id] = language
    return language


def peek_user_language(user_id: int) -> str:
    """The cached language without touching the DB (English until get_user_language has run)."""
    return _USER_LANGUAGE.get(user_id, DEFAULT_LANGUAGE)


def set_user_language(user_id: int, language: str) -> str:
    """Remember the member's catalog language. Blocking. Unknown codes fall back to English."""
    language = language if language in _LANGUAGE_CODES else DEFAULT_LANGUAGE
    _USER_LANGUAGE[user_id] = language
    try:
        with connect_db() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS user_catalog_prefs "
                "(user_id VARCHAR(64) PRIMARY KEY, language VARCHAR(16) NOT NULL, updated_at INTEGER NOT NULL)"
            )
            conn.execute("DELETE FROM user_catalog_prefs WHERE user_id = ?", (str(user_id),))
            conn.execute(
                "INSERT INTO user_catalog_prefs (user_id, language, updated_at) VALUES (?, ?, ?)",
                (str(user_id), language, int(time.time())),
            )
            conn.commit()
    except Exception as exc:
        logger.warning("[CatalogFilters] Could not save the language for %s: %s", user_id, exc)
    return language
