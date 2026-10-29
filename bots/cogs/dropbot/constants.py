"""Constants, Sanrio villager data and the access-denial result type."""

from __future__ import annotations

from typing import Any
from utils.config import Config


# ─── Constants ────────────────────────────────────────────────────────────────

PANEL_EMBED_COLOR      = 0xF80778   # ChoPaeng pink (#f80778)


PANEL_EMBED_COLOR_WARN = 0xFEE75C   # Yellow


PANEL_EMBED_COLOR_ERR  = 0xED4245   # Red


PANEL_FOOTER_TEXT = "ChoPaeng Camp™"


PANEL_FOOTER_ICON = "https://i.ibb.co/HryQtXz/ezgif-1-8a2d930032.gif"


DODO_GIF  = "https://i.ibb.co/wybN7Xn/lg4jVMT.gif"


LOGO_URL  = "https://www.chopaeng.com/assets/logo-C5oO0bbj.webp"


MAP_CDN_BASE = "https://cdn.chopaeng.com/maps"


# Island pages live at <WEBSITE_BASE>/island/<island_name>
WEBSITE_BASE = "https://www.chopaeng.com"


# Limits (enforced for custom item drops)
MAX_DROP_SLOTS = 9      # different items per drop, matches the site's 9-slot Drop Bot


MAX_STACK      = 30     # highest quantity per item


MAX_ITEM_NAME  = 60


# Villager plots: 1 through 10 (matching in-game plots and API backend).
PLOT_MIN = 1


PLOT_MAX = 10


# Sanrio / Amiibo villager injection
SANRIO_EMBED_COLOR = 0xF06292


SANRIO_VILLAGERS: list[tuple[str, str, str]] = [
    ("Chai", "Cinnamoroll • Peppy Elephant", "☕"),
    ("Chelsea", "My Melody • Sweet Deer", "🌸"),
    ("Étoile", "Little Twin Stars • Normal Sheep", "⭐"),
    ("Marty", "Pompompurin • Lazy Bear Cub", "🍮"),
    ("Rilla", "Hello Kitty • Peppy Gorilla", "🎀"),
    ("Toby", "Kerokerokeroppi • Smug Rabbit", "🍵"),
]


SANRIO_NAMES_MAP: dict[str, str] = {
    "chai": "Chai",
    "chelsea": "Chelsea",
    "etoile": "Étoile",
    "étoile": "Étoile",
    "marty": "Marty",
    "rilla": "Rilla",
    "toby": "Toby",
}


SANRIO_VILLAGER_CODES: dict[str, str] = {
    "chai": "villager:elp11",
    "chelsea": "villager:der10",
    "etoile": "villager:shp14",
    "étoile": "villager:shp14",
    "marty": "villager:cbr18",
    "rilla": "villager:gor11",
    "toby": "villager:rbt20",
}


PLACEHOLDER_VILLAGERS: list[tuple[str, str, str]] = [
    ("Raymond", "Smug • Cat (Recommended)", "🐱"),
    ("Marshal", "Smug • Squirrel", "🐿️"),
    ("Shino", "Peppy • Deer", "🦌"),
    ("Sasha", "Lazy • Rabbit", "🐰"),
    ("Judy", "Snooty • Bear cub", "🧸"),
    ("Ankha", "Snooty • Cat", "🐱"),
    ("Lucky", "Lazy • Dog", "🐶"),
]


def is_sanrio_villager(name: str) -> bool:
    """True if name matches one of the Sanrio amiibo characters."""
    return (name or "").strip().lower() in SANRIO_NAMES_MAP


def get_canonical_sanrio_name(name: str) -> str:
    """Returns canonical name for Sanrio character (e.g. 'etoile' -> 'Étoile')."""
    return SANRIO_NAMES_MAP.get((name or "").strip().lower(), (name or "").strip().title())


# Access gates (see _check_access)
ENFORCE_BLOCKLIST = True


# Flip to True once _has_registered_character() is implemented against the real table.
REQUIRE_REGISTERED_CHARACTER = False


SUB_RULES_ROLE_ID = getattr(Config, "SUB_RULES_ROLE_ID", 1077997850165772398) or 1077997850165772398


SUB_RULES_CHANNEL_ID = getattr(Config, "SUB_RULES_CHANNEL_ID", 783677194576330792) or 783677194576330792


ORDER_RULES_ROLE_ID = getattr(Config, "ORDER_RULES_ROLE_ID", 1176191811438510231) or 1176191811438510231


GUILD_ID = getattr(Config, "GUILD_ID", 729590421478703135) or 729590421478703135


class Denied(str):
    """Structured access denial result with an identifiable error code and user-facing message."""
    code: str

    def __new__(cls, code: str, message: str):
        obj = super().__new__(cls, message)
        obj.code = code
        return obj

    @property
    def message(self) -> str:
        return str(self)

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, Denied):
            return self.code == other.code and str(self) == str(other)
        return str(self) == other or self.code == other

    def __hash__(self) -> int:
        return hash((self.code, str(self)))


def _build_sub_rules_msg() -> Denied:
    role_id = getattr(Config, "SUB_RULES_ROLE_ID", 1077997850165772398) or 1077997850165772398
    channel_id = 783677194576330792
    return Denied(
        "sub_rules",
        f"You need to **accept the sub rules** first before you can use the islands (role <@&{role_id}> required).\n"
        f"Please go to <#{channel_id}> to read and accept the sub rules, then come back and try again.",
    )


SUB_RULES_MSG: Denied = _build_sub_rules_msg()


ORDER_RULES_MSG: Denied = Denied(
    "order_rules",
    "You need to **accept the sub rules** first before you can drop items or inject villagers.",
)


# Timings
ACTION_COOLDOWN       = 10.0   # seconds between drops / injections per user+island


ACCESS_CACHE_TTL      = 30.0   # seconds a blocklist verdict is cached


WEBHOOK_DEBOUNCE_TTL  = 3.0    # seconds, per (user, island)


FORCE_EDIT_SECONDS    = 300.0  # re-edit an unchanged panel at least this often (keeps timestamp fresh)


REFRESH_CONCURRENCY   = 4      # parallel panel refreshes


CATALOG_ITEM_LIMIT    = 100


UNKNOWN_ISLAND_MSG = (
    "This panel points to an island I don't recognise. "
    "Ask a moderator to run `/dropbot setup-panel` in this channel."
)


_PLACEHOLDER_DODOS = {"00000", "-----", "GETTIN'", "NONE", "NULL"}


CART_SAVE_DELAY = 1.0   # seconds to coalesce rapid cart edits into one DB write


CART_TTL_SECONDS = 86400
