"""
NHL Island Map Parser Module (v4 - Evidence-Based Multi-Format Detection & Coordinate Resolution)

Locates and parses Animal Crossing: New Horizons .nhl (New Horizons Layer) files:
Paths checked:
  - VILLAGERS_DIR/<island_name>/nhl/maprefresh.nhl       (VIP / Sub Islands)
  - TWITCH_VILLAGERS_DIR/<island_name>/nhl/maprefresh.nhl (Free / Twitch Islands)

Architecture & Design:
  1. Multi-Format Evidence Detection:
     Evaluates candidate binary layouts (16-byte uint32 record, 8-byte NHSE item,
     4-byte compact) against structural markers (0xFFFD, 0xFFFE), catalog hit rates,
     and valid ID distributions instead of assuming a format based merely on file size divisibility.
  2. Coordinate Derivation:
     Evaluates explicit coordinates in record fields vs. row-major linear tile layout.
     Dynamically computes acre dimensions, columns (A..), rows (1..), and prevents silent clamping.
  3. Strict Item Validation:
     Filters low-number noise slots (< 0x50), segregates unconfirmed/unknown IDs into
     `unknown_records` rather than fabricating item names, and only places confirmed catalog items in `items`.
  4. MTime & Size Cache Invalidation:
     Tracks file modification time and file size to automatically invalidate cached map data
     immediately when the file changes on disk, while preserving `force_refresh=True`.
  5. Real-File Enforcement & Diagnostics:
     Strictly parses real files only (no mock data). Provides detailed diagnostics on
     detected format, confidence score, coordinate strategy, and slot counts.
"""

import os
import struct
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any, Tuple

from utils.config import Config

logger = logging.getLogger("NhlMapParser")

_ITEM_CATALOG_CACHE: Optional[Dict[Any, Dict[str, Any]]] = None
_CATALOG_ITEMS_LIST: List[Dict[str, Any]] = []
_CATALOG_ITEMS_BY_NAME: Dict[str, Dict[str, Any]] = {}
_CATALOG_ITEMS_BY_ID: Dict[str, Dict[str, Any]] = {}
_RECIPES_LIST: List[Dict[str, Any]] = []
_CUSTOMIZATIONS_BY_NAME: Dict[str, Dict[str, Any]] = {}
_TREASURE_ISLANDS: List[Dict[str, Any]] = []
_TREASURE_BY_ITEM: Dict[str, List[Dict[str, Any]]] = {}
_TREASURE_BY_ISLAND: Dict[str, List[Dict[str, Any]]] = {}
_MAP_CACHE: Dict[str, Dict[str, Any]] = {}
_CACHE_TTL_SECONDS = 60.0

# Confirmed 16-byte record layout for real .nhl files:
#   offset +0 (uint16): A (raw field / coordinate candidate)
#   offset +2 (uint16): B (raw field / coordinate candidate)
#   offset +4 (uint32): item_id (little-endian uint32)
#   offset +8 (uint16): marker / sentinel field (0xFFFD, 0xFFFE)
#   offset +10 (uint16): padding / reserved
#   offset +12 (uint16): F (raw field)
#   offset +14 (uint16): G (raw field)
RECORD_SIZE_16 = 16
ITEM_ID_OFFSET = 4
ITEM_ID_STRUCT = "<I"
MARKER_OFFSET = 8
MARKER_STRUCT = "<H"

# Observed bulk background pattern markers
BACKGROUND_A_VALUES = {0xFFFD, 0xFFFE}
KNOWN_MARKER_VALUES = {0xFFFD, 0xFFFE}

# Confirmed lowest real ACNH item ID (0x50 = "clackercart").
# Nonzero values below 0x50 are non-item fields (terrain/count/flags)
# and are excluded from catalog lookup to prevent "everything is a painting" collisions.
MIN_PLAUSIBLE_ITEM_ID = 0x50


def _load_item_catalog() -> Dict[Any, Dict[str, Any]]:
    """Load items from data/acnh.min.json (or fallback items_detail.json / acnh.json)
    into a fast lookup table supporting 4-hex, 8-hex, and integer decimal keys.
    """
    global _ITEM_CATALOG_CACHE, _CATALOG_ITEMS_LIST, _CATALOG_ITEMS_BY_NAME, _CATALOG_ITEMS_BY_ID
    global _RECIPES_LIST, _CUSTOMIZATIONS_BY_NAME, _TREASURE_ISLANDS, _TREASURE_BY_ITEM, _TREASURE_BY_ISLAND

    if _ITEM_CATALOG_CACHE is not None:
        return _ITEM_CATALOG_CACHE

    catalog: Dict[Any, Dict[str, Any]] = {}
    items_list: List[Dict[str, Any]] = []
    items_by_name: Dict[str, Dict[str, Any]] = {}
    items_by_id: Dict[str, Dict[str, Any]] = {}
    recipes_list: List[Dict[str, Any]] = []
    customizations_by_name: Dict[str, Dict[str, Any]] = {}

    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # Primary source: data/acnh.min.json
    primary_path = os.path.join(base_dir, "data", "acnh.min.json")
    items_path = primary_path if os.path.exists(primary_path) else None

    if not items_path:
        for fallback in ["items_detail.json", "acnh.json"]:
            cand = os.path.join(base_dir, fallback)
            if os.path.exists(cand):
                items_path = cand
                break

    if not items_path:
        logger.warning("[NhlMapParser] No catalog JSON file found.")
        _ITEM_CATALOG_CACHE = catalog
        return catalog

    def _index_entry(id_val: Any, entry: Dict[str, Any]):
        if id_val is None:
            return
        try:
            if isinstance(id_val, int):
                int_v = id_val
            else:
                s = str(id_val).strip()
                if s.startswith(("0x", "0X")):
                    int_v = int(s, 16)
                elif s.isdigit():
                    int_v = int(s)
                else:
                    int_v = int(s, 16)

            h4 = f"{int_v:04X}"
            h8 = f"{int_v:08X}"
            for k in (h4, h4.lower(), h8, h8.lower(), int_v):
                if k not in catalog:
                    catalog[k] = entry
        except Exception:
            pass

    try:
        with open(items_path, "r", encoding="utf-8", errors="ignore") as f:
            data = json.load(f)

        if isinstance(data, dict) and "items" in data and ("creatures" in data or "recipes" in data):
            # Comprehensive acnh.min.json format
            # 1. Base items and variations
            for item in data.get("items", []):
                name = item.get("name") or item.get("Name")
                if not name:
                    continue
                cat = item.get("sourceSheet") or item.get("category") or item.get("Category") or "Miscellaneous"
                diy = bool(item.get("diy") or item.get("DIY") == "Yes")
                image = (
                    item.get("image")
                    or item.get("storageImage")
                    or item.get("inventoryImage")
                    or item.get("closetImage")
                    or item.get("albumImage")
                    or item.get("framedImage")
                    or ""
                )

                entry = {
                    "name": name,
                    "category": cat,
                    "diy": diy,
                    "imageUrl": image,
                }
                top_id = item.get("internalId") or item.get("Internal ID") or item.get("id")
                _index_entry(top_id, entry)

                # Variations (different colors, remakes, etc.)
                variations_list = []
                for var in item.get("variations", []) or []:
                    var_name = var.get("variation")
                    display_name = (
                        f"{name} ({var_name})"
                        if var_name and str(var_name).lower() not in ("na", "none", "")
                        else name
                    )
                    var_img = (
                        var.get("image")
                        or var.get("storageImage")
                        or var.get("closetImage")
                        or image
                    )
                    v_entry = {
                        "name": display_name,
                        "category": cat,
                        "diy": diy,
                        "imageUrl": var_img,
                    }
                    _index_entry(var.get("internalId") or var.get("variantId"), v_entry)
                    variations_list.append({
                        "variation": var_name or "Standard",
                        "pattern": var.get("pattern"),
                        "image_url": var_img,
                        "colors": var.get("colors") or [],
                        "internal_id": var.get("internalId") or var.get("variantId"),
                        "unique_entry_id": var.get("uniqueEntryId"),
                    })

                # Record full item metadata for /api/items/search & /api/items/customizations
                item_summary = {
                    "name": name,
                    "category": cat,
                    "internal_id": top_id,
                    "diy": diy,
                    "buy": item.get("buy"),
                    "sell": item.get("sell"),
                    "image_url": image,
                    "variations_count": len(variations_list),
                    "colors": item.get("colors") or [],
                    "body_title": item.get("bodyTitle"),
                    "pattern_title": item.get("patternTitle"),
                    "kit_cost": item.get("kitCost"),
                    "kit_type": item.get("kitType"),
                    "cyrus_customize_price": item.get("cyrusCustomizePrice"),
                    "variations": variations_list,
                }
                items_list.append(item_summary)
                items_by_name[name.lower()] = item_summary
                if top_id:
                    items_by_id[str(top_id).lower()] = item_summary

                is_customizable = len(variations_list) > 1 or bool(
                    item.get("bodyTitle") or item.get("patternTitle") or item.get("kitCost")
                )
                customizations_by_name[name.lower()] = {
                    "ok": True,
                    "found": True,
                    "item": name,
                    "category": cat,
                    "internal_id": top_id,
                    "customizable": is_customizable,
                    "body_title": item.get("bodyTitle"),
                    "pattern_title": item.get("patternTitle"),
                    "kit_cost": item.get("kitCost"),
                    "kit_type": item.get("kitType"),
                    "cyrus_customize_price": item.get("cyrusCustomizePrice"),
                    "variations_count": len(variations_list),
                    "variations": variations_list,
                }

            # 2. Creatures (fish, bugs, sea creatures)
            for cr in data.get("creatures", []):
                c_name = cr.get("name")
                if not c_name:
                    continue
                c_cat = cr.get("sourceSheet") or "Creatures"
                c_img = (
                    cr.get("iconImage")
                    or cr.get("critterpediaImage")
                    or cr.get("furnitureImage")
                    or ""
                )
                cr_id = cr.get("internalId")
                _index_entry(
                    cr_id,
                    {"name": c_name, "category": c_cat, "diy": False, "imageUrl": c_img},
                )
                cr_summary = {
                    "name": c_name,
                    "category": c_cat,
                    "internal_id": cr_id,
                    "diy": False,
                    "buy": cr.get("buy"),
                    "sell": cr.get("sell"),
                    "image_url": c_img,
                    "variations_count": 0,
                    "colors": cr.get("colors") or [],
                    "body_title": None,
                    "pattern_title": None,
                    "kit_cost": None,
                    "kit_type": None,
                    "cyrus_customize_price": None,
                    "variations": [],
                }
                items_list.append(cr_summary)
                items_by_name[c_name.lower()] = cr_summary
                if cr_id:
                    items_by_id[str(cr_id).lower()] = cr_summary

            # 3. Recipes
            for rec in data.get("recipes", []):
                r_name = rec.get("name")
                if not r_name:
                    continue
                r_img = rec.get("image") or rec.get("imageSh") or ""
                rec_id = rec.get("internalId")
                _index_entry(
                    rec_id,
                    {"name": f"{r_name} (DIY Recipe)", "category": "Recipes", "diy": True, "imageUrl": r_img},
                )
                recipes_list.append({
                    "name": r_name,
                    "category": rec.get("category") or rec.get("sourceSheet") or "Recipes",
                    "image": r_img,
                    "buy": rec.get("buy"),
                    "sell": rec.get("sell"),
                    "source": rec.get("source") or [],
                    "season_event": rec.get("seasonEvent"),
                    "internal_id": rec_id,
                    "crafted_item_internal_id": rec.get("craftedItemInternalId"),
                    "materials": rec.get("materials") or {},
                    "unique_entry_id": rec.get("uniqueEntryId") or "",
                })

        else:
            # Fallback legacy items_detail.json / acnh.json
            raw_items = data.get("items", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
            for item in raw_items:
                raw_id = item.get("Internal ID") or item.get("pokerId") or item.get("id") or item.get("internalId")
                name = item.get("Name") or item.get("name")
                if not raw_id or not name:
                    continue

                image_url = ""
                variations = item.get("Variations")
                if isinstance(variations, list) and len(variations) > 0:
                    image_url = variations[0].get("imageUrl") or ""

                entry = {
                    "name": name,
                    "category": item.get("Category", "Miscellaneous"),
                    "diy": item.get("DIY") == "Yes",
                    "imageUrl": image_url,
                }
                _index_entry(raw_id, entry)

                if isinstance(variations, list):
                    for var in variations:
                        var_poker = var.get("pokerId")
                        if var_poker:
                            _index_entry(
                                var_poker,
                                {
                                    **entry,
                                    "imageUrl": var.get("imageUrl") or image_url,
                                },
                            )

        # Also load treasure island mappings from items_detail.json if available
        detail_cand = os.path.join(base_dir, "items_detail.json")
        if os.path.exists(detail_cand):
            try:
                with open(detail_cand, "r", encoding="utf-8", errors="ignore") as f2:
                    d2 = json.load(f2)
                    _TREASURE_ISLANDS = d2.get("treasureIslands", [])
                    for ti in _TREASURE_ISLANDS:
                        ti_item = (ti.get("item") or "").strip().lower()
                        ti_isl = (ti.get("islandID") or "").strip().lower()
                        if ti_item:
                            _TREASURE_BY_ITEM.setdefault(ti_item, []).append(ti)
                        if ti_isl:
                            _TREASURE_BY_ISLAND.setdefault(ti_isl, []).append(ti)
            except Exception as exc:
                logger.debug(f"[NhlMapParser] items_detail.json load note: {exc}")

        logger.info(
            f"[NhlMapParser] Loaded {len(catalog)} lookup keys, {len(items_list)} items, "
            f"{len(recipes_list)} recipes, {len(_TREASURE_ISLANDS)} treasure island entries from {items_path}."
        )
    except Exception as exc:
        logger.error(f"[NhlMapParser] Failed to load catalog from {items_path}: {exc}")

    _ITEM_CATALOG_CACHE = catalog
    _CATALOG_ITEMS_LIST = items_list
    _CATALOG_ITEMS_BY_NAME = items_by_name
    _CATALOG_ITEMS_BY_ID = items_by_id
    _RECIPES_LIST = recipes_list
    _CUSTOMIZATIONS_BY_NAME = customizations_by_name
    return catalog


def locate_nhl_file(island_name: str) -> Tuple[Optional[str], bool, List[str]]:
    """
    Locates the maprefresh.nhl file for an island across both:
      - Config.VILLAGERS_DIR (Sub / VIP Islands)
      - Config.TWITCH_VILLAGERS_DIR (Free / Twitch Islands)
    Returns: (found_path, file_exists, checked_expected_paths)
    """
    if not island_name:
        return None, False, []

    clean_name = island_name.strip()
    norm_name = re.sub(r"[^a-zA-Z0-9]", "", clean_name).lower()

    search_roots = [
        Config.VILLAGERS_DIR,
        Config.TWITCH_VILLAGERS_DIR,
    ]

    checked_paths: List[str] = []

    for root in search_roots:
        if not root:
            continue

        direct_path = os.path.join(root, clean_name, "nhl", "maprefresh.nhl")
        checked_paths.append(direct_path)

        if not os.path.exists(root):
            continue

        if os.path.isfile(direct_path):
            return direct_path, True, checked_paths

        # Case-insensitive folder search
        try:
            for folder in os.listdir(root):
                f_path = os.path.join(root, folder)
                if os.path.isdir(f_path):
                    f_norm = re.sub(r"[^a-zA-Z0-9]", "", folder).lower()
                    if f_norm == norm_name:
                        candidate = os.path.join(f_path, "nhl", "maprefresh.nhl")
                        checked_paths.append(candidate)
                        if os.path.isfile(candidate):
                            return candidate, True, checked_paths
        except OSError:
            pass

    return (checked_paths[0] if checked_paths else None), False, checked_paths


def locate_island_nhl_files(
    island_name: str, specific_file: Optional[str] = None
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Locates all .nhl files (or a specific requested .nhl file) for an island across both:
      - Config.VILLAGERS_DIR (Sub / VIP Islands)
      - Config.TWITCH_VILLAGERS_DIR (Free / Twitch Islands)
    Checks both the `nhl/` subfolder and the root folder for the island.
    Returns: (found_files_list, checked_expected_paths)
    """
    if not island_name:
        return [], []

    clean_name = island_name.strip()
    norm_name = re.sub(r"[^a-zA-Z0-9]", "", clean_name).lower()

    search_roots = [
        Config.VILLAGERS_DIR,
        Config.TWITCH_VILLAGERS_DIR,
    ]

    target_filename = None
    if specific_file:
        target_filename = os.path.basename(specific_file.strip())
        if not target_filename.lower().endswith(".nhl"):
            return [], [f"Invalid file extension for {target_filename}"]

    checked_paths: List[str] = []
    found_files: List[Dict[str, Any]] = []
    seen_filenames = set()

    for root in search_roots:
        if not root:
            continue

        direct_island_dir = os.path.join(root, clean_name)
        direct_nhl_dir = os.path.join(direct_island_dir, "nhl")
        checked_paths.append(direct_nhl_dir)

        if not os.path.exists(root):
            continue

        candidate_dirs: List[str] = []
        if os.path.isdir(direct_island_dir):
            candidate_dirs.append(direct_island_dir)

        try:
            for folder in os.listdir(root):
                f_path = os.path.join(root, folder)
                if os.path.isdir(f_path):
                    f_norm = re.sub(r"[^a-zA-Z0-9]", "", folder).lower()
                    if f_norm == norm_name and f_path not in candidate_dirs:
                        candidate_dirs.append(f_path)
        except OSError:
            pass

        for island_dir in candidate_dirs:
            nhl_sub = os.path.join(island_dir, "nhl")
            search_folders = [nhl_sub, island_dir]

            for folder in search_folders:
                if folder not in checked_paths:
                    checked_paths.append(folder)

                if not os.path.isdir(folder):
                    continue

                if target_filename:
                    cand_file = os.path.join(folder, target_filename)
                    if os.path.isfile(cand_file):
                        stat = os.stat(cand_file)
                        return [
                            {
                                "filename": target_filename,
                                "file_path": cand_file,
                                "size_bytes": stat.st_size,
                                "modified_at": stat.st_mtime,
                            }
                        ], checked_paths
                else:
                    try:
                        for entry in os.listdir(folder):
                            if entry.lower().endswith(".nhl"):
                                f_full = os.path.join(folder, entry)
                                if os.path.isfile(f_full) and entry.lower() not in seen_filenames:
                                    stat = os.stat(f_full)
                                    found_files.append(
                                        {
                                            "filename": entry,
                                            "file_path": f_full,
                                            "size_bytes": stat.st_size,
                                            "modified_at": stat.st_mtime,
                                        }
                                    )
                                    seen_filenames.add(entry.lower())
                    except OSError:
                        pass

    if target_filename:
        return [], checked_paths

    def _sort_key(item):
        fname = item["filename"].lower()
        return (0 if fname == "maprefresh.nhl" else 1, fname)

    found_files.sort(key=_sort_key)
    return found_files, checked_paths


def _resolve_grid_dimensions(total_slots: int) -> Tuple[int, int, int, List[str], List[int]]:
    """
    Dynamically determines grid width, height, acre size, column labels, and row numbers
    based on standard ACNH acre layouts.
    """
    if total_slots >= 55296:      # 9 columns x 6 rows of 32x32 acres
        grid_w, grid_h, acre_size = 288, 192, 32
    elif total_slots >= 43008:    # 7 columns x 6 rows of 32x32 acres
        grid_w, grid_h, acre_size = 224, 192, 32
    elif total_slots >= 25600:    # 8 columns x 8 rows of 20x20 acres
        grid_w, grid_h, acre_size = 160, 160, 20
    elif total_slots >= 13824:    # 9 columns x 6 rows of 16x16 acres
        grid_w, grid_h, acre_size = 144, 96, 16
    elif total_slots >= 10752:    # 7 columns x 6 rows of 16x16 acres
        grid_w, grid_h, acre_size = 112, 96, 16
    elif total_slots >= 1024:     # 1 single acre of 32x32
        grid_w, grid_h, acre_size = 32, 32, 32
    elif total_slots >= 256:      # 1 single acre of 16x16
        grid_w, grid_h, acre_size = 16, 16, 16
    else:
        grid_w, grid_h, acre_size = 112, 96, 16

    num_cols = max(1, grid_w // acre_size)
    num_rows = max(1, grid_h // acre_size)
    cols = [chr(ord("A") + c) for c in range(min(num_cols, 26))]
    rows = [r + 1 for r in range(num_rows)]

    return grid_w, grid_h, acre_size, cols, rows


def _coord_to_sector(x: int, y: int, acre_size: int, num_cols: int, num_rows: int) -> str:
    """
    Maps tile (X, Y) to standard ACNH sector (e.g. A1, B3, F6).
    Explicitly handles out-of-bounds coordinates without silent inaccurate clamping.
    """
    if acre_size <= 0:
        acre_size = 16

    col_idx = x // acre_size
    row_idx = y // acre_size

    if col_idx < 0 or col_idx >= num_cols or row_idx < 0 or row_idx >= num_rows:
        return "OUT_OF_BOUNDS"

    col_letter = chr(ord("A") + col_idx)
    row_number = row_idx + 1
    return f"{col_letter}{row_number}"


def _detect_nhl_format(
    raw_bytes: bytes, catalog: Dict[Any, Dict[str, Any]]
) -> Tuple[Optional[str], int, float, List[str]]:
    """
    Evidence-based format detection. Evaluates multiple candidate record layouts:
      1. '16_byte_uint32': 16-byte records, uint32 item ID at offset +4, marker at +8
      2. '8_byte_nhse': 8-byte NHSE items, uint16 item ID at offset +0, FreeParam at +4
      3. '4_byte_compact': 4-byte records, uint16 item ID at offset +0, count at +2

    Returns: (detected_format_name, record_size, confidence_score, reasons_list)
    """
    file_size = len(raw_bytes)
    if file_size == 0:
        return None, 0, 0.0, ["File is empty (0 bytes)."]

    candidates = []

    # Candidate 1: 16-byte record layout
    if file_size >= 16 and file_size % 16 == 0:
        total = file_size // 16
        sample_n = min(total, 5000)
        marker_hits = 0
        cat_hits = 0
        valid_items = 0
        empty_count = 0
        bg_count = 0

        for i in range(sample_n):
            off = i * 16
            A = struct.unpack_from("<H", raw_bytes, off + 0)[0]
            item_id = struct.unpack_from("<I", raw_bytes, off + 4)[0]
            marker = struct.unpack_from("<H", raw_bytes, off + 8)[0]

            if marker in KNOWN_MARKER_VALUES:
                marker_hits += 1
            if A in BACKGROUND_A_VALUES and item_id != 0:
                bg_count += 1
                continue
            if item_id == 0 or item_id in (0xFFFE, 0xFFFF, 0xFEFF, 0xFFFD):
                empty_count += 1
                continue
            if item_id < MIN_PLAUSIBLE_ITEM_ID:
                continue

            valid_items += 1
            h4 = f"{item_id:04X}"
            h8 = f"{item_id:08X}"
            if h4 in catalog or h8 in catalog or item_id in catalog:
                cat_hits += 1

        marker_ratio = marker_hits / sample_n if sample_n else 0
        hit_ratio = cat_hits / valid_items if valid_items else 0
        score = 0.0
        reasons = []

        if marker_ratio >= 0.70:
            score += 0.45 * marker_ratio
            reasons.append(f"Marker field (0xFFFD/0xFFFE at +8) consistency: {marker_ratio*100:.1f}%")
        if valid_items > 0:
            score += 0.45 * hit_ratio
            reasons.append(f"Catalog hit rate: {hit_ratio*100:.1f}% ({cat_hits}/{valid_items})")
        elif (empty_count + bg_count) == sample_n:
            score += 0.40
            reasons.append("Valid empty/background map file structure")

        if file_size in (172032, 688128, 409600):
            score += 0.10
            reasons.append(f"Known standard island file size ({file_size} bytes)")

        candidates.append(("16_byte_uint32", 16, min(1.0, score), reasons))

    # Candidate 2: 8-byte NHSE layout
    if file_size >= 8 and file_size % 8 == 0:
        total = file_size // 8
        sample_n = min(total, 5000)
        sentinel_hits = 0
        cat_hits = 0
        valid_items = 0

        for i in range(sample_n):
            off = i * 8
            item_id = struct.unpack_from("<H", raw_bytes, off + 0)[0]
            if item_id in (0xFFFE, 0xFFFD, 0x0000):
                sentinel_hits += 1
                continue
            if item_id < MIN_PLAUSIBLE_ITEM_ID:
                continue

            valid_items += 1
            h4 = f"{item_id:04X}"
            if h4 in catalog or item_id in catalog:
                cat_hits += 1

        sentinel_ratio = sentinel_hits / sample_n if sample_n else 0
        hit_ratio = cat_hits / valid_items if valid_items else 0
        score = 0.0
        reasons = []

        if sentinel_ratio >= 0.60:
            score += 0.35 * sentinel_ratio
            reasons.append(f"NHSE sentinel (0xFFFE/0xFFFD at +0) consistency: {sentinel_ratio*100:.1f}%")
        if valid_items > 0:
            score += 0.45 * hit_ratio
            reasons.append(f"Catalog hit rate: {hit_ratio*100:.1f}% ({cat_hits}/{valid_items})")

        if file_size in (344064, 442368, 8192):
            score += 0.20
            reasons.append(f"Known NHSE layer dump size ({file_size} bytes)")

        candidates.append(("8_byte_nhse", 8, min(1.0, score), reasons))

    # Candidate 3: 4-byte compact layout
    if file_size >= 4 and file_size % 4 == 0:
        total = file_size // 4
        sample_n = min(total, 5000)
        cat_hits = 0
        valid_items = 0

        for i in range(sample_n):
            off = i * 4
            item_id = struct.unpack_from("<H", raw_bytes, off + 0)[0]
            if item_id in (0x0000, 0xFFFE, 0xFFFF, 0xFEFF):
                continue
            if item_id < MIN_PLAUSIBLE_ITEM_ID:
                continue

            valid_items += 1
            h4 = f"{item_id:04X}"
            if h4 in catalog or item_id in catalog:
                cat_hits += 1

        hit_ratio = cat_hits / valid_items if valid_items else 0
        score = 0.35 * hit_ratio if valid_items > 0 else 0.10
        reasons = [f"Catalog hit rate: {hit_ratio*100:.1f}% ({cat_hits}/{valid_items})"] if valid_items > 0 else []
        candidates.append(("4_byte_compact", 4, min(1.0, score), reasons))

    if not candidates:
        return None, 0, 0.0, [f"File size {file_size} does not match any plausible record alignment."]

    # Select best candidate by score
    candidates.sort(key=lambda x: x[2], reverse=True)
    best_fmt, best_stride, best_score, best_reasons = candidates[0]

    if best_score < 0.25:
        return None, 0, best_score, [
            f"Candidate {best_fmt} failed confidence threshold ({best_score:.2f} < 0.25).",
            *best_reasons,
        ]

    return best_fmt, best_stride, best_score, best_reasons


def parse_nhl_bytes(raw_bytes: bytes, island_name: str) -> Dict[str, Any]:
    """
    Parse raw binary .nhl data into confirmed items, coordinates, sectors, and summary statistics.
    Evidence-based format detection prevents misclassification and excludes false items.
    """
    catalog = _load_item_catalog()
    file_size = len(raw_bytes)

    if file_size == 0:
        return {
            "ok": False,
            "island": island_name,
            "status": "error",
            "file_found": True,
            "error": "NHL file is empty (0 bytes).",
            "items": [],
            "sectors": {},
            "sector_summary": {},
            "stats": {"total_items": 0, "unique_categories": 0, "category_counts": {}},
        }

    # 1. Detect format using evidence
    detected_fmt, stride, confidence, reasons = _detect_nhl_format(raw_bytes, catalog)

    if not detected_fmt or stride <= 0:
        logger.warning(
            f"[NhlMapParser] Failed to detect valid format for {island_name} ({file_size} bytes): {reasons}"
        )
        return {
            "ok": False,
            "island": island_name,
            "status": "error",
            "file_found": True,
            "error": f"Unrecognized NHL binary format: {'; '.join(reasons)}",
            "file_size": file_size,
            "items": [],
            "sectors": {},
            "sector_summary": {},
            "stats": {"total_items": 0, "unique_categories": 0, "category_counts": {}},
        }

    logger.info(
        f"[NhlMapParser] Detected format '{detected_fmt}' (stride={stride}, confidence={confidence*100:.1f}%) for {island_name}."
    )

    total_slots = file_size // stride
    grid_w, grid_h, acre_size, cols, rows = _resolve_grid_dimensions(total_slots)
    num_cols = len(cols)
    num_rows = len(rows)

    # 2. Coordinate strategy analysis
    # Inspect whether record fields A/B provide explicit in-bounds coordinates for valid items
    coordinate_strategy = "linear_grid_row_major"
    if detected_fmt == "16_byte_uint32" and total_slots > 0:
        sample_coords = []
        for i in range(min(total_slots, 1000)):
            off = i * 16
            A = struct.unpack_from("<H", raw_bytes, off + 0)[0]
            B = struct.unpack_from("<H", raw_bytes, off + 2)[0]
            item_id = struct.unpack_from("<I", raw_bytes, off + 4)[0]
            if A not in BACKGROUND_A_VALUES and item_id >= MIN_PLAUSIBLE_ITEM_ID:
                sample_coords.append((A, B))

        if len(sample_coords) >= 10:
            in_bounds_ab = sum(1 for (a, b) in sample_coords if 0 <= a < grid_w and 0 <= b < grid_h)
            if in_bounds_ab / len(sample_coords) >= 0.95 and len(set(sample_coords)) >= len(sample_coords) * 0.80:
                coordinate_strategy = "explicit_record_ab"

    items: List[Dict[str, Any]] = []
    unknown_records: List[Dict[str, Any]] = []
    sectors: Dict[str, List[Dict[str, Any]]] = {}
    category_counts: Dict[str, int] = {}
    total_valid_items = 0
    empty_count = 0
    background_fill_count = 0
    low_value_count = 0
    unresolved_nonzero_count = 0

    for idx in range(total_slots):
        offset = idx * stride
        if offset + stride > file_size:
            break

        if detected_fmt == "16_byte_uint32":
            A = struct.unpack_from("<H", raw_bytes, offset + 0)[0]
            B = struct.unpack_from("<H", raw_bytes, offset + 2)[0]
            item_id = struct.unpack_from(ITEM_ID_STRUCT, raw_bytes, offset + ITEM_ID_OFFSET)[0]
            marker = struct.unpack_from(MARKER_STRUCT, raw_bytes, offset + MARKER_OFFSET)[0]
            F = struct.unpack_from("<H", raw_bytes, offset + 12)[0]
            G = struct.unpack_from("<H", raw_bytes, offset + 14)[0]
            count_or_flag = 1

            if A in BACKGROUND_A_VALUES and item_id != 0:
                background_fill_count += 1
                continue
        elif detected_fmt == "8_byte_nhse":
            item_id = struct.unpack_from("<H", raw_bytes, offset + 0)[0]
            count_or_flag = 1
            A, B, F, G = 0, 0, 0, 0
            marker = 0
        else:  # 4_byte_compact
            item_id = struct.unpack_from("<H", raw_bytes, offset + 0)[0]
            count_or_flag = struct.unpack_from("<H", raw_bytes, offset + 2)[0]
            A, B, F, G = 0, 0, 0, 0
            marker = 0

        # Empty / sentinel slots
        if item_id == 0 or item_id in (0xFFFE, 0xFFFF, 0xFEFF, 0xFFFD):
            empty_count += 1
            continue

        # Exclude noise / low-value IDs below 0x50 that collide with paintings
        if item_id < MIN_PLAUSIBLE_ITEM_ID:
            low_value_count += 1
            continue

        # Derive coordinates
        if coordinate_strategy == "explicit_record_ab":
            tile_x = A
            tile_y = B
        else:
            tile_x = idx % grid_w
            tile_y = idx // grid_w

        sector = _coord_to_sector(tile_x, tile_y, acre_size, num_cols, num_rows)

        hex_id_4 = f"{item_id:04X}"
        hex_id_8 = f"{item_id:08X}"

        # Look up in catalog
        info = catalog.get(hex_id_4) or catalog.get(hex_id_8) or catalog.get(item_id)

        if not info:
            unresolved_nonzero_count += 1
            unknown_records.append({
                "record_index": idx,
                "itemIdDecimal": item_id,
                "itemIdHex": hex_id_8,
                "x": tile_x,
                "y": tile_y,
                "sector": sector,
                "raw_A": A,
                "raw_B": B,
            })
            continue

        name = info["name"]
        category = info.get("category") or "Miscellaneous"
        diy = info.get("diy") or False
        image_url = info.get("imageUrl", "")

        category_counts[category] = category_counts.get(category, 0) + 1
        total_valid_items += 1

        item_obj = {
            "name": name,
            "category": category,
            "diy": diy,
            "internalId": hex_id_4,
            "itemIdHex": hex_id_8,
            "itemIdDecimal": item_id,
            "x": tile_x,
            "y": tile_y,
            "sector": sector,
            "count": count_or_flag if count_or_flag > 1 else 1,
            "imageUrl": image_url,
            "markerField": f"0x{marker:04X}",
            "record_index": idx,
            "raw_A": A,
            "raw_B": B,
            "raw_F": F,
            "raw_G": G,
            "position_confirmed": (coordinate_strategy == "explicit_record_ab"),
        }

        items.append(item_obj)
        if sector not in sectors:
            sectors[sector] = []
        sectors[sector].append(item_obj)

    sector_summary = {}
    for sec, sec_items in sectors.items():
        sec_cat_counts: Dict[str, int] = {}
        for item in sec_items:
            cat = item["category"]
            sec_cat_counts[cat] = sec_cat_counts.get(cat, 0) + 1
        top_cats = sorted(sec_cat_counts.items(), key=lambda x: x[1], reverse=True)[:3]
        sector_summary[sec] = {
            "total_items": len(sec_items),
            "top_categories": [cat for cat, _ in top_cats],
            "sample_items": [i["name"] for i in sec_items[:5]],
        }

    total_resolved = total_valid_items + unresolved_nonzero_count
    resolution_rate = (total_valid_items / total_resolved) if total_resolved > 0 else 1.0

    return {
        "ok": True,
        "island": island_name,
        "status": "live_nhl",
        "file_found": True,
        "record_size_bytes": stride,
        "total_records": total_slots,
        "diagnostics": {
            "detected_format": detected_fmt,
            "confidence": round(confidence, 3),
            "format_reasons": reasons,
            "coordinate_strategy": coordinate_strategy,
            "file_size_bytes": file_size,
        },
        "grid": {
            "width": grid_w,
            "height": grid_h,
            "acre_size": acre_size,
            "cols": cols,
            "rows": rows,
        },
        "stats": {
            "total_items": total_valid_items,
            "unique_categories": len(category_counts),
            "category_counts": category_counts,
            "empty_slots": empty_count,
            "background_fill_slots": background_fill_count,
            "low_value_slots": low_value_count,
            "unknown_id_slots": unresolved_nonzero_count,
            "catalog_resolution_rate": round(resolution_rate, 3),
        },
        "sector_summary": sector_summary,
        "sectors": sectors,
        "items": items,
        "unknown_records": unknown_records[:100],  # capped sample for diagnostic inspection
    }


def get_island_map_data(island_name: str, force_refresh: bool = False) -> Dict[str, Any]:
    """
    Main entrypoint: retrieves parsed map data for an island.
    Uses file mtime and file size validation to automatically invalidate cached data
    when maprefresh.nhl changes on disk. Respects force_refresh=True.
    Strictly parses real files only. Never creates mock data.
    """
    clean_name = island_name.strip()
    norm_key = clean_name.lower()

    file_path, file_exists, checked_paths = locate_nhl_file(clean_name)

    if not file_exists or not file_path or not os.path.isfile(file_path):
        return {
            "ok": False,
            "island": clean_name,
            "status": "not_found",
            "file_found": False,
            "file_path": None,
            "error": f"maprefresh.nhl not found for island '{clean_name}'.",
            "checked_paths": checked_paths,
            "grid": {
                "width": 112,
                "height": 96,
                "acre_size": 16,
                "cols": ["A", "B", "C", "D", "E", "F", "G"],
                "rows": [1, 2, 3, 4, 5, 6],
            },
            "items": [],
            "sectors": {},
            "sector_summary": {},
            "stats": {"total_items": 0, "unique_categories": 0, "category_counts": {}},
        }

    try:
        stat = os.stat(file_path)
        cur_mtime = stat.st_mtime
        cur_size = stat.st_size

        if not force_refresh and norm_key in _MAP_CACHE:
            cached_entry = _MAP_CACHE[norm_key]
            if (
                cached_entry.get("mtime") == cur_mtime
                and cached_entry.get("file_size") == cur_size
                and (time.time() - cached_entry.get("cached_at", 0) < _CACHE_TTL_SECONDS)
            ):
                return cached_entry["data"]

        with open(file_path, "rb") as f:
            raw_bytes = f.read()

        parsed = parse_nhl_bytes(raw_bytes, clean_name)
        parsed["file_path"] = file_path

        _MAP_CACHE[norm_key] = {
            "mtime": cur_mtime,
            "file_size": cur_size,
            "cached_at": time.time(),
            "data": parsed,
        }
        return parsed

    except Exception as exc:
        logger.error(f"[NhlMapParser] Error parsing {file_path}: {exc}")
        return {
            "ok": False,
            "island": clean_name,
            "status": "error",
            "file_found": True,
            "file_path": file_path,
            "error": f"Failed to parse maprefresh.nhl: {exc}",
            "grid": {
                "width": 112,
                "height": 96,
                "acre_size": 16,
                "cols": ["A", "B", "C", "D", "E", "F", "G"],
                "rows": [1, 2, 3, 4, 5, 6],
            },
            "items": [],
            "sectors": {},
            "sector_summary": {},
            "stats": {"total_items": 0, "unique_categories": 0, "category_counts": {}},
        }


def search_catalog_items(
    query: str = "",
    category: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> Dict[str, Any]:
    """Search catalog items by name, category, or internal ID with ranking."""
    _load_item_catalog()
    q = (query or "").strip().lower()
    cat_filter = (category or "").strip().lower()

    if not q and not cat_filter:
        subset = _CATALOG_ITEMS_LIST[offset : offset + limit]
        return {
            "ok": True,
            "query": query,
            "total": len(_CATALOG_ITEMS_LIST),
            "limit": limit,
            "offset": offset,
            "items": subset,
        }

    exact_matches: List[Dict[str, Any]] = []
    prefix_matches: List[Dict[str, Any]] = []
    contains_matches: List[Dict[str, Any]] = []

    for it in _CATALOG_ITEMS_LIST:
        it_cat = (it.get("category") or "").lower()
        if cat_filter and cat_filter not in it_cat:
            continue

        name = (it.get("name") or "").lower()
        internal_id = str(it.get("internal_id") or "").lower()

        if q:
            if name == q or internal_id == q:
                exact_matches.append(it)
            elif name.startswith(q):
                prefix_matches.append(it)
            elif q in name or q in internal_id:
                contains_matches.append(it)
        else:
            contains_matches.append(it)

    all_matched = exact_matches + prefix_matches + contains_matches
    total_count = len(all_matched)
    paginated = all_matched[offset : offset + limit]

    return {
        "ok": True,
        "query": query,
        "category": category,
        "total": total_count,
        "limit": limit,
        "offset": offset,
        "items": paginated,
    }


def search_catalog_recipes(
    query: str = "",
    category: Optional[str] = None,
    material: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> Dict[str, Any]:
    """Search crafting DIY recipes by recipe name, crafted item, or material requirement."""
    _load_item_catalog()
    q = (query or "").strip().lower()
    cat_filter = (category or "").strip().lower()
    mat_filter = (material or "").strip().lower()

    matches = []
    for r in _RECIPES_LIST:
        r_cat = (r.get("category") or "").lower()
        if cat_filter and cat_filter not in r_cat:
            continue

        mats = r.get("materials") or {}
        mat_keys = [str(k).lower() for k in mats.keys()]
        if mat_filter and not any(mat_filter in mk for mk in mat_keys):
            continue

        if q:
            name = (r.get("name") or "").lower()
            name_match = q in name
            mat_match = any(q in mk for mk in mat_keys)
            source_match = any(q in str(s).lower() for s in (r.get("source") or []))
            if not (name_match or mat_match or source_match):
                continue

        matches.append(r)

    total = len(matches)
    paginated = matches[offset : offset + limit]

    return {
        "ok": True,
        "query": query,
        "category": category,
        "material": material,
        "total": total,
        "limit": limit,
        "offset": offset,
        "recipes": paginated,
    }


def get_item_customizations(item_query: str) -> Dict[str, Any]:
    """Retrieve full customization kits, Cyrus price, and variations for an item."""
    _load_item_catalog()
    q = (item_query or "").strip().lower()
    if not q:
        return {"ok": False, "error": "Item query is required", "found": False}

    target = _CUSTOMIZATIONS_BY_NAME.get(q)
    if not target:
        for name_key, entry in _CUSTOMIZATIONS_BY_NAME.items():
            if q in name_key:
                target = entry
                break

    if not target:
        return {"ok": True, "found": False, "item": item_query, "variations": []}

    return target


def search_island_items(
    island_name: Optional[str] = None,
    query: str = "",
    acre: Optional[str] = None,
    category: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> Dict[str, Any]:
    """
    Search for items by name, category, or acre across a specific island or all islands.
    Scans live .nhl files (via get_island_map_data) and treasure island mappings.
    """
    _load_item_catalog()
    q = (query or "").strip().lower()
    acre_filter = (acre or "").strip().lower()
    cat_filter = (category or "").strip().lower()

    target_isl = (island_name or "").strip()
    islands_to_search: List[str] = []

    if target_isl and target_isl.lower() != "all":
        islands_to_search = [target_isl]
    else:
        all_ports = getattr(Config, "ALL_ISLAND_PORT_ORDER", [])
        if all_ports:
            islands_to_search = list(all_ports)
        else:
            discovered = set()
            for root in [Config.VILLAGERS_DIR, Config.TWITCH_VILLAGERS_DIR]:
                if root and os.path.exists(root):
                    try:
                        for d in os.listdir(root):
                            if os.path.isdir(os.path.join(root, d)):
                                discovered.add(d)
                    except OSError:
                        pass
            islands_to_search = sorted(list(discovered))

    seen_keys = set()
    total_matches = []
    files_found = 0

    # 1. Search live .nhl map files
    for isl in islands_to_search:
        map_data = get_island_map_data(isl)
        if not map_data.get("file_found"):
            continue
        files_found += 1
        for item in map_data.get("items", []):
            i_name = (item.get("name") or "").lower()
            i_cat = (item.get("category") or "").lower()
            i_acre = (item.get("acre") or item.get("sector") or "").lower()

            if q and not (q in i_name or q in i_cat or q == i_acre):
                continue
            if acre_filter and acre_filter not in i_acre:
                continue
            if cat_filter and cat_filter not in i_cat:
                continue

            key = (i_name, isl.lower(), i_acre)
            if key not in seen_keys:
                seen_keys.add(key)
                match_entry = dict(item)
                match_entry["island"] = isl
                match_entry["source"] = "live_nhl_map"
                total_matches.append(match_entry)

    # 2. Search treasure islands database from items_detail.json
    ti_candidates = _TREASURE_ISLANDS
    if target_isl and target_isl.lower() != "all":
        ti_candidates = _TREASURE_BY_ISLAND.get(target_isl.lower(), [])
        if not ti_candidates:
            ti_candidates = [t for t in _TREASURE_ISLANDS if t.get("islandID", "").lower() == target_isl.lower()]

    for ti in ti_candidates:
        item_name = (ti.get("item") or "").strip()
        item_isl = (ti.get("islandID") or "").strip()
        item_acre = (ti.get("acre") or "").strip()
        item_type = (ti.get("itemtype") or "").strip()

        if q and q not in item_name.lower():
            continue
        if acre_filter and acre_filter not in item_acre.lower():
            continue
        if cat_filter and cat_filter not in item_type.lower():
            continue

        key = (item_name.lower(), item_isl.lower(), item_acre.lower())
        if key not in seen_keys:
            seen_keys.add(key)
            total_matches.append({
                "name": item_name,
                "island": item_isl,
                "acre": item_acre,
                "sector": item_acre.upper(),
                "x": None,
                "y": None,
                "category": item_type or "Catalog",
                "item_id": None,
                "source": "treasure_islands_db",
            })

    total_count = len(total_matches)
    paginated = total_matches[offset : offset + limit]

    return {
        "ok": True,
        "query": query,
        "island": target_isl or "all",
        "file_found": files_found > 0,
        "islands_scanned": len(islands_to_search),
        "islands_with_maps": files_found,
        "total": total_count,
        "total_matches": total_count,
        "limit": limit,
        "offset": offset,
        "items": paginated,
        "matches": paginated,
    }


def get_island_map_telemetry() -> Dict[str, Any]:
    """Return health & freshness diagnostics for .nhl island maps across all configured ports."""
    all_ports = getattr(Config, "ALL_ISLAND_PORT_ORDER", [])
    result = {}
    maps_found = 0
    total_items = 0

    for port_name in all_ports:
        clean = port_name.strip()
        file_path, exists, _ = locate_nhl_file(clean)
        if exists and file_path:
            maps_found += 1
            try:
                stat = os.stat(file_path)
                mtime_iso = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
                size = stat.st_size
            except Exception:
                mtime_iso = None
                size = 0

            cached = _MAP_CACHE.get(clean.lower())
            item_cnt = len(cached["data"].get("items", [])) if cached else None
            if item_cnt:
                total_items += item_cnt

            result[clean] = {
                "has_map": True,
                "file_path": file_path,
                "file_size": size,
                "mtime": mtime_iso,
                "cached_items_count": item_cnt,
            }
        else:
            result[clean] = {
                "has_map": False,
                "file_path": None,
                "file_size": 0,
                "mtime": None,
                "cached_items_count": 0,
            }

    return {
        "total_islands_checked": len(all_ports),
        "islands_with_maps": maps_found,
        "total_cached_items": total_items,
        "islands": result,
    }