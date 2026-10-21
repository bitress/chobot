"""
Data Manager Module
Handles all Google Sheets and villager data operations
Shared across all bots and APIs
"""

import os
import time
import logging
import threading
import re
import json
from datetime import datetime
from typing import Optional, Union, List, Dict, Any, Tuple
try:
    import gspread
except ImportError:
    gspread = None

from utils.config import Config
from utils.helpers import clean_text

logger = logging.getLogger("DataManager")
CACHE_FILE = "cache_dump.json"

class DataManager:
    """Centralized data management for items and villagers"""

    def __init__(self, workbook_name, json_keyfile, cache_refresh_hours=1):
        self.workbook_name = workbook_name
        self.json_keyfile = json_keyfile
        self.cache_refresh_hours = cache_refresh_hours

        self.cache = {}  # Item cache
        self.last_update = None
        self.last_refresh_attempt = None
        self.last_refresh_status = "not_started"
        self.last_refresh_error = None
        self.gc = None
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.image_cache = {}
        self._villager_cache = {}     # {frozenset(dirs): data}
        self._villager_cache_time = None
        self._villager_cache_ttl = 300  # 5 minutes
        self.source = "disk_cache" if os.path.exists(CACHE_FILE) else "none"

        self._connect_sheets()
        self.load_image_catalog()

        # NEW: Try to load local cache immediately
        self.load_local_cache()

        # Start auto-refresh in background thread
        self.refresh_thread = threading.Thread(target=self.auto_refresh_loop, daemon=True)
        self.refresh_thread.start()

    def _connect_sheets(self):
        """Connect to Google Sheets API"""
        try:
            self.gc = gspread.service_account(filename=self.json_keyfile)
            logger.info("Google Sheets client initialized.")
        except Exception as e:
            logger.error(f"Failed to initialize Google Sheets client: {e}")

    def load_image_catalog(self):
        """Load ACNH item images from JSON catalog"""
        try:
            with open("acnh.json", "r", encoding="utf-8") as f:
                data = json.load(f)

            count = 0
            for category, cat_data in data.items():
                for item in cat_data.get("images", []):
                    name = item.get("name")
                    url = item.get("url")

                    if name and url:
                        key = self.normalize_text(name)
                        if key not in self.image_cache:
                            self.image_cache[key] = url
                            count += 1

            logger.info(f"Image Catalog Loaded: {count} images mapped.")
        except FileNotFoundError:
            logger.warning("acnh.json not found! Images will not display.")
        except Exception as e:
            logger.error(f"Failed to load image catalog: {e}")

    def normalize_text(self, s: str) -> str:
        """Normalize text for searching"""
        s = s.lower().strip()
        s = re.sub(r"[^\w\s]", " ", s)
        s = re.sub(r"\s+", " ", s).strip()
        return s

    def load_local_cache(self):
        """Load cache from local JSON file to avoid API latency"""
        if os.path.exists(CACHE_FILE):
            try:
                with open(CACHE_FILE, "r", encoding="utf-8") as f:
                    self.cache = json.load(f)
                self.last_update = datetime.now()
                logger.info(f"[CACHE] Loaded {len(self.cache)} items from disk.")
            except Exception as e:
                logger.error(f"[CACHE] Failed to load local dump: {e}")

    def save_local_cache(self):
        """Save current cache to local JSON file"""
        try:
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(self.cache, f, ensure_ascii=False, indent=2)
            logger.info("[CACHE] Data saved to disk.")
        except Exception as e:
            logger.error(f"[CACHE] Failed to save dump: {e}")

    def update_cache_from_nhl(self) -> bool:
        """
        Scan all configured and discovered island .nhl layer files,
        parse real placed items, and build the item location index.
        The resulting cache stores item locations strictly as island names
        (e.g. 'Bonita, Dalangin') with NO coordinates or sectors, matching
        the exact format used across Discord, Twitch, and Web APIs.
        """
        from utils.nhl_map_parser import get_island_map_data, locate_nhl_file

        logger.info("[NHL] Scanning island .nhl files to build item index...")
        self.last_refresh_attempt = datetime.now()
        self.last_refresh_status = "running"
        self.last_refresh_error = None

        candidate_islands = set()

        for isl in getattr(Config, "SUB_ISLANDS", []):
            if isl:
                candidate_islands.add(isl.strip())
        for isl in getattr(Config, "FREE_ISLANDS", []):
            if isl:
                candidate_islands.add(isl.strip())

        for base_dir in [getattr(Config, "VILLAGERS_DIR", None), getattr(Config, "TWITCH_VILLAGERS_DIR", None)]:
            if base_dir and os.path.exists(base_dir):
                try:
                    for entry in os.listdir(base_dir):
                        if os.path.isdir(os.path.join(base_dir, entry)):
                            candidate_islands.add(entry.strip())
                except OSError:
                    pass

        if not candidate_islands:
            logger.warning("[NHL] No island directories or configured islands found.")
            return False

        temp_cache = {}
        display_map = {}
        islands_indexed = 0
        total_items_found = 0

        known_lookup = {}
        for isl in getattr(Config, "SUB_ISLANDS", []) + getattr(Config, "FREE_ISLANDS", []):
            known_lookup[clean_text(isl)] = isl

        for island_name in sorted(candidate_islands):
            filepath, exists, _ = locate_nhl_file(island_name)
            if not exists or not filepath:
                continue

            try:
                map_data = get_island_map_data(island_name)
                if not map_data.get("ok"):
                    continue

                items = map_data.get("items", [])
                if not items:
                    continue

                canonical_name = known_lookup.get(clean_text(island_name), island_name.title())
                islands_indexed += 1
                island_item_count = 0

                for it in items:
                    name = it.get("name")
                    if not name:
                        continue

                    # Filter out sentinel / corrupted names
                    if name.startswith("Item #6553") or name.startswith("Item #0"):
                        continue

                    island_item_count += 1
                    total_items_found += 1

                    # 1. Full item name (e.g. "Royal Crown", "Ironwood Chair (Walnut)")
                    key = self.normalize_text(name)
                    if key not in display_map:
                        display_map[key] = name

                    if key in temp_cache:
                        current_locs = temp_cache[key].split(", ")
                        if canonical_name not in current_locs:
                            temp_cache[key] += f", {canonical_name}"
                    else:
                        temp_cache[key] = canonical_name

                    # 2. Base variation name if name contains parentheses e.g. "Ironwood Chair"
                    if "(" in name and ")" in name:
                        base_name = re.sub(r"\s*\([^)]*\)", "", name).strip()
                        if base_name:
                            base_key = self.normalize_text(base_name)
                            if base_key != key:
                                if base_key not in display_map:
                                    display_map[base_key] = base_name
                                if base_key in temp_cache:
                                    cur_locs = temp_cache[base_key].split(", ")
                                    if canonical_name not in cur_locs:
                                        temp_cache[base_key] += f", {canonical_name}"
                                else:
                                    temp_cache[base_key] = canonical_name

                    # 3. If DIY, index both item name and "<name> diy" / "<name> recipe"
                    if it.get("diy"):
                        for suffix in ["diy", "recipe"]:
                            alt_key = self.normalize_text(f"{name} {suffix}")
                            if alt_key not in display_map:
                                display_map[alt_key] = f"{name} {suffix.upper()}"
                            if alt_key in temp_cache:
                                cur_locs = temp_cache[alt_key].split(", ")
                                if canonical_name not in cur_locs:
                                    temp_cache[alt_key] += f", {canonical_name}"
                            else:
                                temp_cache[alt_key] = canonical_name

                logger.info(f"[NHL] Indexed {island_item_count} items from island '{canonical_name}'")

            except Exception as exc:
                logger.error(f"[NHL] Error reading island '{island_name}': {exc}")

        if temp_cache:
            temp_cache["_display"] = display_map
            with self.lock:
                self.cache = temp_cache
                self.last_update = datetime.now()
                self.last_refresh_status = "ok"
                self.last_refresh_error = None
                self.source = "nhl"
            self.save_local_cache()
            logger.info(
                f"[NHL] Index complete: {len(temp_cache) - 1} unique items indexed "
                f"across {islands_indexed} islands ({total_items_found} total placed items)."
            )
            return True

        logger.warning("[NHL] Scan finished but no valid items were found in .nhl files.")
        return False

    def _update_cache_from_sheets(self) -> bool:
        """Fetch items from Google Sheets"""
        logger.info("Updating cache from Google Sheets...")
        self.last_refresh_attempt = datetime.now()
        self.last_refresh_status = "running"
        self.last_refresh_error = None

        if not self.gc:
            self._connect_sheets()

        if not self.gc:
            self.last_refresh_status = "error"
            self.last_refresh_error = "Google Sheets client not initialized"
            return False

        try:
            wb = self.gc.open(self.workbook_name)
            worksheets = wb.worksheets()
            temp_cache = {}
            display_map = {}
            sheets_scanned = 0
            sheets_failed = 0

            logger.info(f"Found {len(worksheets)} sheets. Scanning...")

            for sheet in worksheets:
                try:
                    rows = sheet.get_all_values()
                    if not rows:
                        continue

                    location_name = sheet.title

                    for row in rows:
                        for cell in row:
                            item_name = cell.strip()
                            if item_name:
                                key = self.normalize_text(item_name)

                                # Store display name
                                if key not in display_map:
                                    display_map[key] = item_name

                                # Store location
                                if key in temp_cache:
                                    current_locations = temp_cache[key].split(", ")
                                    if location_name not in current_locations:
                                        temp_cache[key] += f", {location_name}"
                                else:
                                    temp_cache[key] = location_name

                    sheets_scanned += 1
                    logger.info(f"Indexed: {location_name}")
                    # Small delay to respect rate limits
                    time.sleep(2.0)

                except Exception as e:
                    sheets_failed += 1
                    logger.error(f"Error reading '{sheet.title}': {e}")

            temp_cache["_display"] = display_map

            new_item_count = sum(1 for k in temp_cache if k != "_display")
            with self.lock:
                old_item_count = sum(1 for k in self.cache if k != "_display")

            sufficient = (
                sheets_failed == 0
                or old_item_count == 0
                or new_item_count >= old_item_count
            )

            if sheets_scanned > 0 and new_item_count > 0 and sufficient:
                with self.lock:
                    self.cache = temp_cache
                    self.last_update = datetime.now()
                    self.source = "google_sheets"

                self.save_local_cache()
                self.last_refresh_status = "ok"
                logger.info(
                    f"Scan complete. {new_item_count} items loaded from "
                    f"{sheets_scanned} sheets ({sheets_failed} failed)."
                )
                return True
            else:
                logger.warning(
                    f"Cache refresh produced insufficient data "
                    f"({new_item_count} new vs {old_item_count} existing items, "
                    f"{sheets_scanned}/{len(worksheets)} sheets scanned, "
                    f"{sheets_failed} failed). Keeping existing cache."
                )
                self.last_refresh_status = "degraded"
                self.last_refresh_error = "Refresh produced insufficient data; kept existing cache."
                return False

        except Exception as e:
            logger.error(f"Workbook fetch failed: {e}")
            self.last_refresh_status = "error"
            self.last_refresh_error = str(e)
            self.gc = None  # Force reconnect on next attempt
            return False

    def update_cache(self, force_source=None) -> bool:
        """
        Refresh cache.
        Checks NHL files first by default (ITEM_DATA_SOURCE='nhl' or 'auto').
        If NHL files are present, builds item index from .nhl maps.
        Otherwise falls back to Google Sheets.
        """
        source_mode = force_source or getattr(Config, "ITEM_DATA_SOURCE", "nhl").strip().lower()

        if source_mode in ("nhl", "auto"):
            success = self.update_cache_from_nhl()
            if success:
                return True
            if source_mode == "nhl":
                logger.info("[DATA] NHL scan found no files; preserving current cache.")
                return False

        # Fallback or explicit Google Sheets mode
        return self._update_cache_from_sheets()

    def auto_refresh_loop(self):
        """
        Background thread to manage cache refresh.
        For NHL data, the cache is fixed and persists on disk/in-memory,
        avoiding unnecessary re-scans.
        """
        source_mode = getattr(Config, "ITEM_DATA_SOURCE", "nhl").strip().lower()
        if source_mode == "nhl":
            logger.info("[DATA] NHL data source configured; cache is fixed in-memory.")
            return

        while not self.stop_event.wait(3600 * self.cache_refresh_hours):
            self.update_cache()

    def stop_auto_refresh(self, timeout: float = 5.0):
        """Signal the background refresh loop to stop and wait briefly."""
        self.stop_event.set()
        if self.refresh_thread.is_alive():
            self.refresh_thread.join(timeout=timeout)

    def get_villagers(self, villagers_dirs=None):
        """Fetch villager mapping directly from SysBot HTTP API (cached in memory).
        Does NOT read Villagers.txt from disk to avoid file lock contention with SysBot.
        """
        now = time.time()
        if (
            self._villager_cache_time is not None
            and now - self._villager_cache_time < self._villager_cache_ttl
        ):
            return self._villager_cache

        data = {}
        try:
            from concurrent.futures import ThreadPoolExecutor
            from utils.config import Config
            from utils.sysbot_api import get_island_sysbot_client

            islands = getattr(Config, "ALL_ISLAND_PORT_ORDER", []) or []
            if not islands:
                islands = getattr(Config, "SUB_ISLANDS", []) or [
                    "Hiraya", "Tagumpay", "Bahaghari", "Alapaap", "Bituin",
                    "Lakan", "Galak", "Aruga", "Likha", "Dalisay",
                    "Marahuyo", "Diwa", "Bonita", "Gabay", "Dakila",
                    "Kalangitan", "Malaya", "Pangarap", "Giliw", "Adhika"
                ]

            def _fetch_island(isl_name):
                try:
                    client = get_island_sysbot_client(isl_name, timeout=2)
                    resp = client.get_villagers_sync()
                    if isinstance(resp, dict):
                        raw = resp.get("villagers") or resp.get("data") or []
                        if isinstance(raw, list):
                            return isl_name, [str(v).strip() for v in raw if str(v).strip()]
                except Exception:
                    pass
                return isl_name, []

            with ThreadPoolExecutor(max_workers=min(16, max(len(islands), 1))) as executor:
                for isl_name, v_list in executor.map(_fetch_island, islands):
                    loc_clean = isl_name.strip().title()
                    for v_name in v_list:
                        clean_name = v_name.strip()
                        if not clean_name or len(clean_name) > 30:
                            continue
                        if clean_name in ["Ren?E", "Ren?e"]:
                            clean_name = "Renée"
                        key = clean_name.lower()
                        if key in data:
                            current_locs = [x.strip() for x in data[key].split(",")]
                            if loc_clean not in current_locs:
                                data[key] += f", {loc_clean}"
                        else:
                            data[key] = loc_clean

            self._villager_cache = data
            self._villager_cache_time = now
            return data
        except Exception as e:
            logger.error(f"SysBot villager fetch failed: {e}")
            return self._villager_cache or {}


# ═══════════════════════════════════════════════════════════════════════════════
# Item & Villager Hex Resolution Support (matching Command Builder & SysBot)
# ═══════════════════════════════════════════════════════════════════════════════

_items_catalog_lock = threading.Lock()
_villagers_catalog_lock = threading.Lock()
_cached_items_catalog = None
_cached_villagers_catalog = None


def _find_catalog_file(filename: str) -> Optional[str]:
    """Search for catalog file across project root, current directory, and data dir."""
    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(current_dir)
    candidates = [
        os.path.join(project_root, filename),
        os.path.join(project_root, "data", filename),
        os.path.join(os.getcwd(), filename),
        os.path.join(os.getcwd(), "data", filename),
        os.path.join(current_dir, filename),
    ]
    for path in candidates:
        if os.path.exists(path) and os.path.isfile(path):
            return path
    return None


def _to_hex4(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, int):
        return f"{val:04X}"
    s = str(val).strip()
    if s.isdigit():
        return f"{int(s):04X}"
    if re.fullmatch(r"[0-9a-fA-F]{1,4}", s):
        return s.upper().zfill(4)
    return s.upper()


def load_items_catalog() -> dict:
    """Load and index acnh.min.json (and fallback items_detail.json) for fast O(1) hex lookups."""
    global _cached_items_catalog
    if _cached_items_catalog is not None:
        return _cached_items_catalog

    with _items_catalog_lock:
        if _cached_items_catalog is not None:
            return _cached_items_catalog

        items_by_name = {}
        items_by_norm = {}
        items_list = []

        # 1. Primary: Load data/acnh.min.json
        min_path = _find_catalog_file("acnh.min.json")
        if min_path:
            try:
                with open(min_path, "r", encoding="utf-8") as f:
                    min_data = json.load(f)
                raw_items = min_data.get("items", []) if isinstance(min_data, dict) else min_data
                for it in raw_items:
                    name = (it.get("name") or "").strip()
                    if not name:
                        continue
                    norm = re.sub(r"[^a-z0-9]", "", name.lower())
                    cat = it.get("sourceSheet") or ""

                    vars_list = []
                    if it.get("variations"):
                        for v in it["variations"]:
                            iid = v.get("internalId") or it.get("internalId")
                            var_id = v.get("variantId")
                            raw_id = str(var_id) if var_id is not None else "NA"
                            vars_list.append({
                                "Variation": v.get("variation") or "NA",
                                "Pattern": v.get("pattern") or "NA",
                                "id": raw_id,
                                "pokerId": _to_hex4(iid),
                            })
                    else:
                        iid = it.get("internalId")
                        var_id = it.get("variantId")
                        raw_id = str(var_id) if var_id is not None else "NA"
                        vars_list.append({
                            "Variation": it.get("variation") or "NA",
                            "Pattern": it.get("pattern") or "NA",
                            "id": raw_id,
                            "pokerId": _to_hex4(iid),
                        })

                    base_hex = vars_list[0]["pokerId"] if vars_list else ""
                    entry = {
                        "Name": name,
                        "Category": cat,
                        "Internal ID": base_hex,
                        "Variations": vars_list,
                    }
                    items_list.append(entry)
                    items_by_name[name.lower()] = entry
                    if norm not in items_by_norm:
                        items_by_norm[norm] = entry
                logger.info(f"[DataManager] Loaded {len(items_list)} items from {min_path}")
            except Exception as exc:
                logger.error(f"[DataManager] Failed to load acnh.min.json: {exc}")

        # 2. Fallback / Merge: items_detail.json
        catalog_path = _find_catalog_file("items_detail.json")
        if catalog_path:
            try:
                with open(catalog_path, "r", encoding="utf-8") as f:
                    raw_data = json.load(f)
                dt_items = raw_data.get("items", []) if isinstance(raw_data, dict) else raw_data
                for it in dt_items:
                    name = (it.get("Name") or "").strip()
                    if not name:
                        continue
                    norm = re.sub(r"[^a-z0-9]", "", name.lower())
                    if name.lower() not in items_by_name:
                        items_by_name[name.lower()] = it
                        items_list.append(it)
                    if norm not in items_by_norm:
                        items_by_norm[norm] = it
                logger.info(f"[DataManager] Merged items from {catalog_path}")
            except Exception as exc:
                logger.error(f"[DataManager] Failed to merge items_detail.json: {exc}")

        _cached_items_catalog = {
            "items": items_list,
            "by_name": items_by_name,
            "by_norm": items_by_norm,
        }
        return _cached_items_catalog


def load_villagers_catalog() -> dict:
    """Load and index villagers from acnh.min.json and villagers.json."""
    global _cached_villagers_catalog
    if _cached_villagers_catalog is not None:
        return _cached_villagers_catalog

    with _villagers_catalog_lock:
        if _cached_villagers_catalog is not None:
            return _cached_villagers_catalog

        villagers_by_name = {}
        villagers_by_id = {}

        # 1. Primary: Load villagers from data/acnh.min.json
        min_path = _find_catalog_file("acnh.min.json")
        if min_path:
            try:
                with open(min_path, "r", encoding="utf-8") as f:
                    min_data = json.load(f)
                for v in min_data.get("villagers", []):
                    v_name = (v.get("name") or "").strip()
                    code = (v.get("filename") or "").strip().lower()
                    if code:
                        villagers_by_id[code] = {"id": code, "name": v_name}
                    if v_name:
                        v_lower = v_name.lower()
                        villagers_by_name[v_lower] = {"id": code, "name": v_name}
                        villagers_by_name[re.sub(r"[^a-z0-9]", "", v_lower)] = {"id": code, "name": v_name}
                        try:
                            import unicodedata
                            decomposed = unicodedata.normalize('NFKD', v_lower)
                            stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
                            if stripped != v_lower:
                                villagers_by_name[stripped] = {"id": code, "name": v_name}
                                villagers_by_name[re.sub(r"[^a-z0-9]", "", stripped)] = {"id": code, "name": v_name}
                        except Exception:
                            pass
                logger.info(f"[DataManager] Loaded {len(villagers_by_id)} villagers from {min_path}")
            except Exception as exc:
                logger.error(f"[DataManager] Failed to load villagers from acnh.min.json: {exc}")

        # 2. Fallback / Merge: villagers.json
        v_path = _find_catalog_file("villagers.json")
        if v_path:
            try:
                with open(v_path, "r", encoding="utf-8") as f:
                    raw_v = json.load(f)
                if isinstance(raw_v, dict):
                    for k, v in raw_v.items():
                        v_id = str(v.get("id") or "").strip().lower()
                        v_name = str(v.get("name") or k).strip().lower()
                        if v_id and v_id not in villagers_by_id:
                            villagers_by_id[v_id] = v
                        if v_name and v_name not in villagers_by_name:
                            villagers_by_name[v_name] = v
                            norm_name = re.sub(r"[^a-z0-9]", "", v_name)
                            villagers_by_name[norm_name] = v
                logger.info(f"[DataManager] Merged villagers from {v_path}")
            except Exception as exc:
                logger.error(f"[DataManager] Failed to merge villagers.json: {exc}")

        _cached_villagers_catalog = {
            "by_name": villagers_by_name,
            "by_id": villagers_by_id,
        }
        return _cached_villagers_catalog


def generate_full_item_hex(
    base_id: Union[str, int, None],
    variant_string: Optional[Union[str, int]] = None,
    category: str = "",
) -> str:
    """
    Build the final order hex for SysBot / Discord order & drop commands.
    Direct Python implementation of chopaeng's commandBuilderHex.ts.
    - If base_id or variant_string is already 16-character hex, returns as-is.
    - For customizable furniture, encodes variant info (primary/secondary) into a 16-char hex: 0000<variantHex>0000<baseId>.
    - For Fencing, uses fencing layout: <countHex>00310000<baseId>.
    - For standalone items or items with distinct color internal IDs, returns the 4-char hex ID.
    """
    clean_base = str(base_id or "").strip().upper()
    clean_var = str(variant_string).strip() if variant_string is not None else ""

    # Handle compound id like '116F:2B0C' or '38EF:1_0' if passed as base_id
    if ":" in clean_base:
        parts = clean_base.split(":", 1)
        parent, v_key = parts[0], parts[1]
        if re.match(r"^\d+_\d+$", v_key):
            clean_base = parent
            if not clean_var or clean_var == "NA":
                clean_var = v_key
        elif re.match(r"^[0-9A-F]{2,6}$", v_key, re.IGNORECASE):
            clean_base = v_key
            if clean_var == v_key:
                clean_var = "NA"
        else:
            clean_base = parent

    if len(clean_base) == 16:
        return clean_base
    if clean_var and len(clean_var) == 16:
        return clean_var.upper()

    # If no variant or variant is NA / empty / DIY:
    if not clean_var or clean_var in ("NA", "", "DIY"):
        return clean_base.zfill(4)

    # Pattern variations (customizable items like furniture) use "primary_secondary" (e.g. "0_0", "1_0")
    var_parts = clean_var.split("_")
    if len(var_parts) == 2 and var_parts[0].isdigit() and var_parts[1].isdigit():
        primary = int(var_parts[0])
        secondary = int(var_parts[1])
        padded_base = clean_base.zfill(4)

        if category == "Fencing":
            return f"{primary:X}00310000{padded_base}"

        variant_int = primary + (secondary * 32)
        variant_hex = f"{variant_int:04X}"
        return f"0000{variant_hex}0000{padded_base}"

    # If clean_var was passed a hex ID (like '2B0C')
    if re.match(r"^[0-9A-F]{2,6}$", clean_var, re.IGNORECASE):
        return clean_var.zfill(4).upper()

    return clean_base.zfill(4)


def resolve_item_hex(item_query: str) -> Optional[str]:
    """
    Resolve any item name or variant or raw code to its 4-character or 16-character hex code.
    Examples:
      - '2154' -> '2154'
      - '00000001000038EF' -> '00000001000038EF'
      - 'ABD' -> '00000000000038EF' (or '38EF')
      - 'Blue ABD' or 'ABD (Blue)' -> '00000001000038EF'
      - 'Faux-shearling boots' -> '2D1E'
      - 'Faux-shearling boots (Beige)' -> '1271'
    """
    query = str(item_query or "").strip()
    if not query:
        return None

    # Check if raw hex code (4 hex or 16 hex digits, optional 0x prefix)
    clean_hex = re.sub(r"^0x", "", query, flags=re.IGNORECASE).strip()
    if re.fullmatch(r"[0-9a-fA-F]{4}|[0-9a-fA-F]{16}", clean_hex):
        return clean_hex.upper()

    # Strip common prefixes like 'regular ', 'default ', 'normal '
    clean_prefix = re.sub(r"^(?:regular|normal|default)\s+", "", query, flags=re.IGNORECASE).strip()
    if clean_prefix and clean_prefix != query:
        res = resolve_item_hex(clean_prefix)
        if res:
            return res

    cat_data = load_items_catalog()
    items_by_name = cat_data.get("by_name", {})
    items_by_norm = cat_data.get("by_norm", {})
    all_items = cat_data.get("items", [])

    # Check for parentheses: 'ABD (Blue)' or 'Faux-shearling boots (Beige)'
    m_paren = re.match(r"^(.*?)\s*\((.*?)\)$", query)
    base_name = None
    var_name = None
    if m_paren:
        base_name = m_paren.group(1).strip()
        var_name = m_paren.group(2).strip()

    target_item = items_by_name.get(query.lower()) or items_by_norm.get(re.sub(r"[^a-z0-9]", "", query.lower()))

    # If direct query didn't match and no parens, check variation prefix or suffix, e.g. 'Blue ABD' or 'ABD Blue'
    if not target_item and not base_name:
        for it in all_items:
            it_name_lower = (it.get("Name") or "").lower()
            variations = it.get("Variations") or []
            for v in variations:
                v_name = str(v.get("Variation") or "").lower().strip()
                if v_name and v_name != "na":
                    if query.lower() in (f"{v_name} {it_name_lower}", f"{it_name_lower} {v_name}"):
                        target_item = it
                        var_name = str(v.get("Variation"))
                        break
            if target_item:
                break

    if base_name and not target_item:
        target_item = items_by_name.get(base_name.lower()) or items_by_norm.get(re.sub(r"[^a-z0-9]", "", base_name.lower()))

    if not target_item:
        return None

    variations = target_item.get("Variations") or []
    cat = target_item.get("Category", "")
    base_id = str(target_item.get("Internal ID") or "").strip().upper()

    matched_var = None
    if var_name and variations:
        var_norm = var_name.lower().strip()
        for v in variations:
            v_val = str(v.get("Variation") or "").strip().lower()
            p_val = str(v.get("Pattern") or "").strip().lower()
            if v_val == var_norm or p_val == var_norm:
                matched_var = v
                break

    if not matched_var and variations:
        # If no specific variant was found or asked, use first variation
        matched_var = variations[0]

    if matched_var:
        raw_id = str(matched_var.get("id") or "").strip()
        is_pattern = bool(re.match(r"^\d+_\d+$", raw_id))
        if is_pattern:
            return generate_full_item_hex(matched_var.get("pokerId") or base_id, raw_id, cat)
        else:
            hex_id = matched_var.get("pokerId") or (matched_var.get("id") if matched_var.get("id") != "NA" else None) or base_id
            return str(hex_id).upper().zfill(4)

    return base_id.upper().zfill(4)


def resolve_villager_code(villager_query: str) -> str:
    """
    Resolve any villager name, code, or injection command to the standard 'villager:<internal_id>' format.
    Examples:
      - 'Agent S' -> 'villager:squ05'
      - 'agent s' -> 'villager:squ05'
      - 'squ05' -> 'villager:squ05'
      - 'villager:squ05' -> 'villager:squ05'
      - '!injectvillager squ05' -> 'villager:squ05'
    """
    query = str(villager_query or "").strip()
    query = re.sub(r"^!(?:injectvillager|inject|mvi)\s*", "", query, flags=re.IGNORECASE).strip()

    if query.lower().startswith("villager:"):
        raw_code = query[9:].strip().lower()
        query = raw_code

    v_cat = load_villagers_catalog()
    villagers_by_id = v_cat.get("by_id", {})
    villagers_by_name = v_cat.get("by_name", {})

    # Check direct ID match
    if query.lower() in villagers_by_id:
        return f"villager:{query.lower()}"

    # Explicit Sanrio Amiibo mapping
    sanrio_map = {
        "chai": "elp11",
        "chelsea": "der10",
        "etoile": "shp14",
        "étoile": "shp14",
        "marty": "cbr18",
        "rilla": "gor11",
        "toby": "rbt20",
    }
    if query.lower() in sanrio_map:
        return f"villager:{sanrio_map[query.lower()]}"

    # Check name match
    norm = re.sub(r"[^a-z0-9]", "", query.lower())
    found = villagers_by_name.get(query.lower()) or villagers_by_name.get(norm)
    if found:
        return f"villager:{str(found.get('id') or '').lower()}"

    # Return as villager:<query> if unmapped
    return f"villager:{query.lower()}"


def resolve_drop_items(
    raw_items: Union[str, List[str]],
    max_slots: int = 40,
) -> tuple[str, list[str]]:
    """
    Parse and convert drop items into space-delimited hex codes with NO COMMAS.
    Repeats items for multiple counts or variants (e.g. regular abd, blue abd, Faux-shearling boots 10x).
    Returns (space_separated_string, list_of_hexes).
    """
    if not raw_items:
        return "", []

    entries: list[str] = []
    if isinstance(raw_items, str):
        clean_text = raw_items.strip()
        if clean_text.lower().startswith("!drop"):
            clean_text = re.sub(r"^!drop\s*", "", clean_text, flags=re.IGNORECASE).strip()

        # Check if already a space-delimited sequence of raw hex codes
        tokens = [t.strip() for t in clean_text.split() if t.strip()]
        if tokens and all(re.fullmatch(r"[0-9a-fA-F]{4}(?:[0-9a-fA-F]{12})?", t) for t in tokens):
            entries = tokens
        elif any(sep in clean_text for sep in [",", "\n", ";", "\r"]):
            entries = [e.strip() for e in re.split(r"[,;\n\r]+", clean_text) if e.strip()]
        else:
            entries = [clean_text]
    elif isinstance(raw_items, list):
        entries = [str(x).strip() for x in raw_items if str(x).strip()]

    result_hexes: list[str] = []

    for entry in entries:
        if not entry:
            continue

        # Check if token is already pure hex
        if re.fullmatch(r"[0-9a-fA-F]{4}|[0-9a-fA-F]{16}", entry):
            result_hexes.append(entry.upper())
            continue

        # Extract name and quantity
        # e.g. 'Faux-shearling boots 10x', 'Gold nugget 30', 'ABD x2'
        name = entry
        qty = 1

        m_x = re.match(r"^(.*?)(?:\s*[xX×]\s*(\d+)|\s+(\d+)[xX×])$", entry)
        if m_x:
            name = m_x.group(1).strip()
            qty = int(m_x.group(2) or m_x.group(3) or 1)
        else:
            m_num = re.match(r"^(.*?)\s+(\d+)$", entry)
            if m_num:
                cand_name = m_num.group(1).strip()
                cand_qty = int(m_num.group(2))
                resolved_cand = resolve_item_hex(cand_name)
                if resolved_cand or cand_qty <= 40:
                    name = cand_name
                    qty = cand_qty

        qty = max(1, min(qty, max_slots))
        hex_code = resolve_item_hex(name)
        if not hex_code:
            clean_name = re.sub(r"[^0-9a-fA-F]", "", name)
            if len(clean_name) in (4, 16):
                hex_code = clean_name.upper()
            else:
                hex_code = name

        for _ in range(qty):
            result_hexes.append(hex_code)
            if len(result_hexes) >= max_slots:
                break

        if len(result_hexes) >= max_slots:
            break

    result_str = " ".join(result_hexes)
    return result_str, result_hexes


# Attach static methods to DataManager class for object-level access
DataManager.generate_full_item_hex = staticmethod(generate_full_item_hex)
DataManager.resolve_item_hex = staticmethod(resolve_item_hex)
DataManager.resolve_villager_code = staticmethod(resolve_villager_code)
DataManager.resolve_drop_items = staticmethod(resolve_drop_items)

