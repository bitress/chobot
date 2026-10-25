"""
ACNH Catalog Module
Loads and indexes orderable items, DIY recipes, and villagers from data/acnh.min.json.
Excludes non-orderables (achievements, reactions, construction, creatures, npcs, seasons/events).
"""

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Set, Tuple
try:
    from thefuzz import fuzz
except ImportError:
    import difflib

    class _FuzzFallback:
        @staticmethod
        def ratio(s1, s2):
            return int(difflib.SequenceMatcher(None, str(s1), str(s2)).ratio() * 100)

        @staticmethod
        def partial_ratio(s1, s2):
            s1, s2 = str(s1), str(s2)
            if not s1 or not s2:
                return 0
            if s1 in s2 or s2 in s1:
                return 100
            return int(difflib.SequenceMatcher(None, s1, s2).ratio() * 100)

    fuzz = _FuzzFallback()

logger = logging.getLogger("ACNHCatalog")

ORDERABLE_SOURCE_SHEETS = {
    "Housewares", "Miscellaneous", "Wall-mounted", "Ceiling Decor",
    "Artwork", "Photos", "Posters", "Gyroids", "Music", "Fossils",
    "Tools/Goods", "Fencing", "Wallpaper", "Floors", "Rugs",
    "Tops", "Bottoms", "Dress-Up", "Headwear", "Accessories",
    "Socks", "Shoes", "Bags", "Umbrellas", "Clothing Other",
    "Other", "Message Cards",
}


def generate_full_item_hex(
    base_id: Optional[Any],
    variant_string: Optional[Any] = None,
    category: str = "",
) -> str:
    """
    Builds the final 16-character order hex.
    - If baseId or variantString is already a full 16-char hex, return it as-is.
    - Otherwise pad the base id and encode variant info (primary/secondary) into the string.
    - "Fencing" category uses a different byte layout than everything else.
    """
    if base_id is None or base_id == "":
        return ""

    base_str = str(base_id).strip().upper()
    var_str = str(variant_string or "").strip().upper()

    if len(base_str) == 16:
        return base_str
    if len(var_str) == 16:
        return var_str

    padded_base_id = base_str.zfill(4)

    if not var_str or var_str in ("NA", "NONE", "", "DIY"):
        return padded_base_id

    primary = 0
    secondary = 0
    parts = var_str.split("_")
    if len(parts) == 2:
        try:
            primary = int(parts[0])
            secondary = int(parts[1])
        except (ValueError, TypeError):
            primary = 0
            secondary = 0

    if category.strip().lower() == "fencing":
        primary_hex = f"{primary:X}"
        return f"{primary_hex}00310000{padded_base_id}"

    variant_int = primary + (secondary * 32)
    variant_hex = f"{variant_int:04X}"
    return f"0000{variant_hex}0000{padded_base_id}"


class CatalogItem:
    """Represents an orderable item or DIY recipe."""
    def __init__(
        self,
        name: str,
        category: str,
        internal_id: Optional[str] = None,
        image_url: Optional[str] = None,
        variation: Optional[str] = None,
        variant_id: Optional[str] = None,
        stack_size: int = 1,
        diy: bool = False,
        raw_data: Optional[Dict[str, Any]] = None,
        series: Optional[str] = None,
    ):
        self.name = name
        self.category = category
        self.internal_id = internal_id or ""
        self.image_url = image_url or ""
        self.variation = variation or ""
        self.variant_id = variant_id or ""
        self.stack_size = stack_size
        self.diy = diy
        self.raw_data = raw_data or {}
        self.series = series or ""

    @property
    def display_name(self) -> str:
        if self.variation and self.variation.lower() not in ("na", "none", ""):
            return f"{self.name} ({self.variation})"
        return self.name

    def to_hex(self) -> str:
        """Generate the full 16-character / 4-character hex code for this item."""
        if not self.internal_id:
            return ""
        return generate_full_item_hex(
            base_id=self.internal_id,
            variant_string=self.variant_id or self.variation,
            category=self.category,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "category": self.category,
            "internal_id": self.internal_id,
            "hex_code": self.to_hex(),
            "image_url": self.image_url,
            "variation": self.variation,
            "variant_id": self.variant_id,
            "stack_size": self.stack_size,
            "diy": self.diy,
            "series": self.series,
        }


class CatalogVillager:
    """Represents an orderable villager."""
    def __init__(
        self,
        name: str,
        villager_id: str,
        species: str = "",
        personality: str = "",
        icon_url: Optional[str] = None,
        photo_url: Optional[str] = None,
        raw_data: Optional[Dict[str, Any]] = None,
    ):
        self.name = name
        self.villager_id = villager_id  # e.g., 'brd09', 'cat00'
        self.species = species
        self.personality = personality
        self.icon_url = icon_url or ""
        self.photo_url = photo_url or ""
        self.raw_data = raw_data or {}

    @property
    def emoji(self) -> str:
        return SPECIES_EMOJI.get(self.species, "🐱")

    @property
    def display_name(self) -> str:
        return f"{self.name} ({self.species})"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "villager_id": self.villager_id,
            "species": self.species,
            "personality": self.personality,
            "emoji": self.emoji,
            "icon_url": self.icon_url,
            "photo_url": self.photo_url,
        }


# ─── Preset & Popular Data ───────────────────────────────────────────────────

SPECIES_EMOJI: Dict[str, str] = {
    "Alligator": "🐊",
    "Anteater": "🐜",
    "Bear": "🐻",
    "Bear cub": "🧸",
    "Bird": "🐦",
    "Bull": "🐂",
    "Cat": "🐱",
    "Chicken": "🐔",
    "Cow": "🐄",
    "Deer": "🦌",
    "Dog": "🐶",
    "Duck": "🦆",
    "Eagle": "🦅",
    "Elephant": "🐘",
    "Frog": "🐸",
    "Goat": "🐐",
    "Gorilla": "🦍",
    "Hamster": "🐹",
    "Hippo": "🦛",
    "Horse": "🐴",
    "Kangaroo": "🦘",
    "Koala": "🐨",
    "Lion": "🦁",
    "Monkey": "🐵",
    "Mouse": "🐭",
    "Octopus": "🐙",
    "Ostrich": "🦤",
    "Penguin": "🐧",
    "Pig": "🐷",
    "Rabbit": "🐰",
    "Rhinoceros": "🦏",
    "Sheep": "🐑",
    "Squirrel": "🐿️",
    "Tiger": "🐯",
    "Wolf": "🐺",
}

POPULAR_DROP_PRESETS: List[Tuple[str, str, str, str]] = [
    ("Royal Crown x40", "12M Bells max inventory drop", ", ".join(["Royal Crown"] * 40), "👑"),
    ("Gold Nuggets x30", "Full stack of 30 gold crafting nuggets", "Gold nugget 30", "🪙"),
    ("Nook Miles Tickets x30", "Full stack of 30 travel tickets", "Nook Miles Ticket 30", "🎫"),
    ("Max Bells (3.96M)", "40 bags of 99,000 Bells", ", ".join(["1E33"] * 40), "💰"),
    ("Fish Bait x30", "Full stack of 30 fishing bait", "Fish bait 30", "🐟"),
    ("Golden Tools Set", "All 6 tools (Axe, Shovel, Net, Rod, Can, Slingshot)", "Golden axe, Golden shovel, Golden net, Golden rod, Golden watering can, Golden slingshot", "🛠️"),
    ("Wood Stacks (30 each)", "Normal Wood, Hardwood, Softwood (90 total)", "Wood 30, Hardwood 30, Softwood 30", "🪵"),
    ("Iron Nuggets x30", "Full stack of 30 iron nuggets", "Iron nugget 30", "🪨"),
    ("Clay x30", "Full stack of 30 crafting clay", "Clay 30", "🧱"),
    ("Stone x30", "Full stack of 30 crafting stone", "Stone 30", "🪨"),
    ("Star Fragments x30", "Full stack of 30 yellow star fragments", "Star fragment 30", "⭐"),
    ("Large Star Fragments x10", "Full stack of 10 large star fragments", "Large star fragment 10", "🌟"),
    ("Pearls x30", "Full stack of 30 mermaid pearls", "Pearl 30", "💎"),
    ("Glowing Moss x30", "Full stack of 30 glowing moss", "Glowing moss 30", "✨"),
    ("Vines x30", "Full stack of 30 climbing vines", "Vine 30", "🌿"),
    ("Cherry-Blossom Petals x30", "Full stack of 30 sakura petals", "Cherry-blossom petal 30", "🌸"),
    ("Round Mushrooms x30", "Full stack of 30 forest mushrooms", "Round mushroom 30", "🍄"),
    ("Young Spring Bamboo x30", "Full stack of 30 bamboo", "Young spring bamboo 30", "🎋"),
]

POPULAR_VILLAGER_NAMES: List[str] = [
    "Raymond", "Marshal", "Shino", "Sasha", "Ione", "Ankha",
    "Sherb", "Audie", "Judy", "Dom", "Stitches", "Marina",
    "Zucker", "Bob", "Coco", "Lucky", "Fauna", "Roald",
    "Tia", "Merengue", "Maple", "Beau", "Lolly", "Tangy", "Molly",
]

POPULAR_VILLAGERS: List[Tuple[str, str, str]] = [
    ("Raymond", "Smug • Cat", "🐱"),
    ("Marshal", "Smug • Squirrel", "🐿️"),
    ("Shino", "Peppy • Deer", "🦌"),
    ("Sasha", "Lazy • Rabbit", "🐰"),
    ("Ione", "Normal • Squirrel", "🐿️"),
    ("Ankha", "Snooty • Cat", "🐱"),
    ("Sherb", "Lazy • Goat", "🐐"),
    ("Audie", "Peppy • Wolf", "🐺"),
    ("Judy", "Snooty • Bear cub", "🧸"),
    ("Dom", "Jock • Sheep", "🐑"),
    ("Stitches", "Lazy • Bear cub", "🧸"),
    ("Marina", "Normal • Octopus", "🐙"),
    ("Zucker", "Lazy • Octopus", "🐙"),
    ("Bob", "Lazy • Cat", "🐱"),
    ("Coco", "Normal • Rabbit", "🐰"),
    ("Lucky", "Lazy • Dog", "🐶"),
    ("Fauna", "Normal • Deer", "🦌"),
    ("Roald", "Jock • Penguin", "🐧"),
    ("Tia", "Normal • Elephant", "🐘"),
    ("Merengue", "Normal • Rhinoceros", "🦏"),
    ("Maple", "Normal • Bear cub", "🧸"),
    ("Beau", "Lazy • Deer", "🦌"),
    ("Lolly", "Normal • Cat", "🐱"),
    ("Tangy", "Peppy • Cat", "🐱"),
    ("Molly", "Normal • Duck", "🦆"),
]

POPULAR_ITEM_NAMES: List[str] = [
    "Royal Crown", "Gold Nugget", "Nook Miles Ticket", "99,000 Bells",
    "Fish Bait", "Golden Axe", "Golden Shovel", "Golden Net", "Golden Rod",
    "Golden Watering Can", "Golden Slingshot", "Wood", "Hardwood", "Softwood",
    "Iron Nugget", "Clay", "Stone", "Star Fragment", "Large Star Fragment", "Pearl",
    "Glowing Moss", "Vine", "Cherry-Blossom Petal", "Round Mushroom",
]


class ACNHCatalog:
    """Centralized catalog manager for ACNH orderables."""
    _instance: Optional["ACNHCatalog"] = None

    def __init__(self, data_path: Optional[str] = None):
        self.data_path = data_path or self._find_data_path()
        self.items: List[CatalogItem] = []
        self.recipes: List[CatalogItem] = []
        self.villagers: List[CatalogVillager] = []

        self._items_by_name: Dict[str, List[CatalogItem]] = {}
        self._items_by_series: Dict[str, List[CatalogItem]] = {}
        self._series_list: List[str] = []
        self._villagers_by_name: Dict[str, CatalogVillager] = {}
        self._villagers_by_id: Dict[str, CatalogVillager] = {}
        self._categories: List[str] = []

        self.loaded = False
        self.load_catalog()

    @classmethod
    def get_instance(cls, data_path: Optional[str] = None) -> "ACNHCatalog":
        if cls._instance is None:
            cls._instance = cls(data_path)
        return cls._instance

    def _find_data_path(self) -> str:
        candidates = [
            os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "acnh.min.json"),
            os.path.join(os.path.dirname(__file__), "..", "data", "acnh.min.json"),
            os.path.abspath("data/acnh.min.json"),
            os.path.abspath("acnh.min.json"),
        ]
        for path in candidates:
            if os.path.exists(path):
                return path
        return os.path.abspath("data/acnh.min.json")

    @staticmethod
    def normalize_text(text: str) -> str:
        if not text:
            return ""
        s = text.lower().strip()
        s = re.sub(r"[^\w\s]", " ", s)
        s = re.sub(r"\s+", " ", s).strip()
        return s

    def load_catalog(self) -> bool:
        if not os.path.exists(self.data_path):
            logger.error(f"[ACNHCatalog] Catalog file not found at: {self.data_path}")
            return False

        try:
            logger.info(f"[ACNHCatalog] Loading data from {self.data_path}...")
            with open(self.data_path, "r", encoding="utf-8") as f:
                raw_data = json.load(f)

            # 1. Load Orderable Items
            raw_items = raw_data.get("items", [])
            categories_set = set()
            for it in raw_items:
                source_sheet = it.get("sourceSheet") or "Other"
                if source_sheet not in ORDERABLE_SOURCE_SHEETS:
                    continue

                categories_set.add(source_sheet)
                name = it.get("name") or "Unknown"
                raw_int_id = it.get("internalId")
                if raw_int_id is None and it.get("variations"):
                    raw_int_id = it["variations"][0].get("internalId")

                if raw_int_id is not None:
                    try:
                        internal_id = f"{int(raw_int_id):04X}"
                    except (ValueError, TypeError):
                        internal_id = str(raw_int_id).strip().upper().zfill(4)
                else:
                    internal_id = ""

                image_url = it.get("image") or ""
                if not image_url and it.get("variations"):
                    v0 = it["variations"][0]
                    image_url = v0.get("image") or v0.get("storageImage") or v0.get("closetImage") or ""

                variation = it.get("variation") or ""
                if not variation and it.get("variations"):
                    variation = it["variations"][0].get("variation") or ""

                variant_id = str(it.get("variantId") or "") if it.get("variantId") is not None else ""
                if not variant_id and it.get("variations"):
                    v_id_val = it["variations"][0].get("variantId")
                    if v_id_val is not None:
                        variant_id = str(v_id_val)

                diy = bool(it.get("diy"))
                stack_size = int(it.get("stackSize") or 1)
                series = str(it.get("series") or "").strip()

                item_obj = CatalogItem(
                    name=name,
                    category=source_sheet,
                    internal_id=internal_id,
                    image_url=image_url,
                    variation=variation,
                    variant_id=variant_id,
                    stack_size=stack_size,
                    diy=diy,
                    raw_data=it,
                    series=series,
                )
                self.items.append(item_obj)

                norm_name = self.normalize_text(name)
                if norm_name not in self._items_by_name:
                    self._items_by_name[norm_name] = []
                self._items_by_name[norm_name].append(item_obj)

                if series:
                    norm_s = self.normalize_text(series)
                    if norm_s not in self._items_by_series:
                        self._items_by_series[norm_s] = []
                    self._items_by_series[norm_s].append(item_obj)

            # 2. Load DIY Recipes
            raw_recipes = raw_data.get("recipes", [])
            categories_set.add("Recipes")
            for rec in raw_recipes:
                name = rec.get("name") or "Unknown"
                raw_int_id = rec.get("internalId")
                if raw_int_id is not None:
                    try:
                        rec_hex = f"{int(raw_int_id):04X}"
                        internal_id = f"0000{rec_hex}000016A2"
                    except (ValueError, TypeError):
                        internal_id = str(raw_int_id).strip().upper()
                else:
                    internal_id = ""

                image_url = rec.get("image") or rec.get("imageSh") or ""
                series = str(rec.get("series") or "").strip()
                item_obj = CatalogItem(
                    name=f"{name} (DIY Recipe)",
                    category="Recipes",
                    internal_id=internal_id,
                    image_url=image_url,
                    stack_size=1,
                    diy=True,
                    raw_data=rec,
                    series=series,
                )
                self.recipes.append(item_obj)

                norm_name = self.normalize_text(name)
                norm_recipe_name = self.normalize_text(f"{name} recipe")
                for key in (norm_name, norm_recipe_name):
                    if key not in self._items_by_name:
                        self._items_by_name[key] = []
                    self._items_by_name[key].append(item_obj)

                if series:
                    norm_s = self.normalize_text(series)
                    if norm_s not in self._items_by_series:
                        self._items_by_series[norm_s] = []
                    self._items_by_series[norm_s].append(item_obj)

            self._series_list = sorted(list(self._items_by_series.keys()))

            # 3. Load Villagers
            raw_villagers = raw_data.get("villagers", [])
            for v in raw_villagers:
                name = v.get("name") or "Unknown"
                v_id = str(v.get("filename") or v.get("uniqueEntryId") or "").lower().strip()
                species = v.get("species") or ""
                personality = v.get("personality") or ""
                icon_url = v.get("iconImage") or ""
                photo_url = v.get("photoImage") or ""

                villager_obj = CatalogVillager(
                    name=name,
                    villager_id=v_id,
                    species=species,
                    personality=personality,
                    icon_url=icon_url,
                    photo_url=photo_url,
                    raw_data=v,
                )
                self.villagers.append(villager_obj)

                norm_name = self.normalize_text(name)
                self._villagers_by_name[norm_name] = villager_obj
                if v_id:
                    self._villagers_by_id[v_id] = villager_obj

            self._categories = sorted(list(categories_set))
            self.loaded = True
            logger.info(
                f"[ACNHCatalog] Successfully loaded {len(self.items)} items, "
                f"{len(self.recipes)} recipes, and {len(self.villagers)} villagers."
            )
            return True
        except Exception as e:
            logger.error(f"[ACNHCatalog] Failed to load catalog: {e}", exc_info=True)
            return False

    def get_categories(self) -> List[str]:
        return list(self._categories)

    def get_villager(self, query: str) -> Optional[CatalogVillager]:
        """Find a villager by exact name or ID."""
        if not query:
            return None
        clean = query.strip().lower()
        if clean in self._villagers_by_id:
            return self._villagers_by_id[clean]
        norm = self.normalize_text(query)
        if norm in self._villagers_by_name:
            return self._villagers_by_name[norm]
        
        # Strip 'villager:' prefix if provided
        if clean.startswith("villager:"):
            sub = clean[9:].strip()
            if sub in self._villagers_by_id:
                return self._villagers_by_id[sub]
            sub_norm = self.normalize_text(sub)
            if sub_norm in self._villagers_by_name:
                return self._villagers_by_name[sub_norm]

        # Fuzzy fallback for villagers
        best_match = None
        best_score = 0
        for v in self.villagers:
            score = fuzz.ratio(norm, self.normalize_text(v.name))
            if score > best_score and score >= 80:
                best_score = score
                best_match = v
        return best_match

    def search_villagers(self, query: str, limit: int = 15) -> List[CatalogVillager]:
        """Search villagers by name, species, or personality."""
        if not query:
            return self.villagers[:limit]
        norm_q = self.normalize_text(query)

        exact_matches = []
        prefix_matches = []
        fuzzy_matches = []

        for v in self.villagers:
            v_norm = self.normalize_text(v.name)
            v_species = self.normalize_text(v.species)
            if v_norm == norm_q or v.villager_id.lower() == norm_q:
                exact_matches.append(v)
            elif v_norm.startswith(norm_q) or norm_q in v_species:
                prefix_matches.append(v)
            else:
                score = fuzz.partial_ratio(norm_q, v_norm)
                if score >= 75:
                    fuzzy_matches.append((score, v))

        fuzzy_sorted = [v for _, v in sorted(fuzzy_matches, key=lambda x: x[0], reverse=True)]
        results = exact_matches + prefix_matches + fuzzy_sorted
        # Deduplicate
        seen = set()
        deduped = []
        for r in results:
            if r.villager_id not in seen:
                seen.add(r.villager_id)
                deduped.append(r)
        return deduped[:limit]

    def get_item(self, name: str) -> Optional[CatalogItem]:
        """Get an item by exact normalized name."""
        norm = self.normalize_text(name)
        items = self._items_by_name.get(norm)
        if items:
            return items[0]
        return None

    def search_items(
        self,
        query: str,
        limit: int = 25,
        category: Optional[str] = None,
        include_recipes: bool = True,
    ) -> List[CatalogItem]:
        """Search items and DIY recipes by keyword with category filtering."""
        if not query:
            source = self.items + (self.recipes if include_recipes else [])
            if category:
                source = [it for it in source if it.category.lower() == category.lower()]
            return source[:limit]

        norm_q = self.normalize_text(query)
        dataset = self.items + (self.recipes if include_recipes else [])
        if category and category.lower() != "all":
            dataset = [it for it in dataset if it.category.lower() == category.lower()]

        exact_matches = []
        prefix_matches = []
        contains_matches = []
        fuzzy_matches = []

        for it in dataset:
            it_norm = self.normalize_text(it.name)
            it_series = self.normalize_text(it.series) if it.series else ""
            if it_norm == norm_q or (it_series and (norm_q == it_series or norm_q in it_series or it_series in norm_q)):
                exact_matches.append(it)
            elif it_norm.startswith(norm_q) or (it_series and it_series.startswith(norm_q)):
                prefix_matches.append(it)
            elif norm_q in it_norm or (it_series and norm_q in it_series):
                contains_matches.append(it)
            else:
                score = fuzz.partial_ratio(norm_q, it_norm)
                if score >= 78:
                    fuzzy_matches.append((score, it))

        fuzzy_sorted = [it for _, it in sorted(fuzzy_matches, key=lambda x: x[0], reverse=True)]
        combined = exact_matches + prefix_matches + contains_matches + fuzzy_sorted

        seen = set()
        deduped = []
        for it in combined:
            key = (it.name, it.variation, it.category)
            if key not in seen:
                seen.add(key)
                deduped.append(it)
        return deduped[:limit]

    def get_popular_series(self) -> List[Tuple[str, str, str]]:
        """
        Dynamically return popular catalog series.
        Returns list of (display_label, search_keyword, emoji).
        """
        preferred: List[Tuple[str, str, str]] = [
            ("Cherry Blossom", "cherry blossom", "🌸"),
            ("Mushroom", "mushroom", "🍄"),
            ("Celeste & Star", "star", "⭐"),
            ("Ironwood", "ironwood", "🪵"),
            ("Golden Series", "golden", "👑"),
            ("Spooky", "spooky", "🎃"),
            ("Festive & Frozen", "festive", "🎄"),
            ("Mermaid", "mermaid", "🧜"),
            ("Sanrio", "sanrio", "🎀"),
            ("Glowing Moss & Vine", "glowing moss", "🌿"),
        ]
        return preferred

    def get_pocket_bundles(self) -> List[Tuple[str, str, str, str]]:
        """
        Return curated and official pocket bundles from DataManager and database:
        list of (name, description, drop_code, emoji).
        """
        from utils.data_manager import get_pocket_bundles
        return get_pocket_bundles()

    def get_drop_presets(self) -> List[Tuple[str, str, str, str]]:
        """Return popular item drop presets: list of (label, description, drop_code, emoji)."""
        return list(POPULAR_DROP_PRESETS)

    def get_popular_villagers(self, limit: int = 25) -> List[CatalogVillager]:
        """Return curated popular villagers as CatalogVillager instances with .emoji support."""
        res: List[CatalogVillager] = []
        for name in POPULAR_VILLAGER_NAMES:
            v = self.get_villager(name)
            if v and v not in res:
                res.append(v)
            if len(res) >= limit:
                break
        if len(res) < limit:
            for v in self.villagers:
                if v not in res:
                    res.append(v)
                if len(res) >= limit:
                    break
        if not res:
            for item in POPULAR_VILLAGERS[:limit]:
                name, desc, emoji_char = item[0], item[1], item[2]
                parts = [p.strip() for p in desc.split("•")]
                pers = parts[0] if len(parts) > 0 else ""
                spec = parts[1] if len(parts) > 1 else ""
                res.append(CatalogVillager(name=name, villager_id=name.lower(), species=spec, personality=pers))
        return res

    def get_popular_villager_names(self, limit: int = 25) -> List[str]:
        """Return names of popular villagers."""
        return [v.name for v in self.get_popular_villagers(limit=limit)]

    def get_popular_item_names(self, limit: int = 25) -> List[str]:
        """Return names of popular drop items."""
        return list(POPULAR_ITEM_NAMES[:limit])

    def get_popular_items(self, limit: int = 25) -> List[CatalogItem]:
        """Return CatalogItem objects for popular items (if found in catalog)."""
        res: List[CatalogItem] = []
        for name in POPULAR_ITEM_NAMES:
            it = self.get_item(name)
            if it and it not in res:
                res.append(it)
            if len(res) >= limit:
                break
        return res

    @staticmethod
    def get_species_emoji(species: str) -> str:
        """Return emoji for a villager species."""
        return SPECIES_EMOJI.get(species, "🐱")

