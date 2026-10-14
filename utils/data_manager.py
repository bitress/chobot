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
