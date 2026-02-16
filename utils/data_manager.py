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
import gspread

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
        self.gc = None
        self.lock = threading.Lock()
        self.image_cache = {}
        self._villager_cache = {}     # {frozenset(dirs): data}
        self._villager_cache_time = None
        self._villager_cache_ttl = 300  # 5 minutes

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

    def update_cache(self):
        """Fetch items from Google Sheets"""
        logger.info("Updating cache from Google Sheets...")

        if not self.gc:
            self._connect_sheets()

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

            # Guard: only replace cache if we actually got meaningful data
            if sheets_scanned > 0 and len(temp_cache) > 1:
                with self.lock:
                    self.cache = temp_cache
                    self.last_update = datetime.now()

                self.save_local_cache()
                logger.info(
                    f"Scan complete. {len(temp_cache)} items loaded from "
                    f"{sheets_scanned} sheets ({sheets_failed} failed)."
                )
            else:
                logger.warning(
                    f"Cache refresh produced too few items "
                    f"({len(temp_cache)} items from {sheets_scanned}/{len(worksheets)} sheets, "
                    f"{sheets_failed} failed). Keeping existing cache ({len(self.cache)} items)."
                )

        except Exception as e:
            logger.error(f"Workbook fetch failed: {e}")
            self.gc = None  # Force reconnect on next attempt

    def auto_refresh_loop(self):
        """Background thread to auto-refresh cache"""
        while True:
            # Wait for the interval (hours * 3600 seconds)
            time.sleep(3600 * self.cache_refresh_hours)
            self.update_cache()

    def get_villagers(self, villagers_dirs):
        """Scan villager text files from provided directories (cached for 5 min)"""
        paths_to_scan = tuple(sorted(p for p in villagers_dirs if p and os.path.exists(p)))

        if not paths_to_scan:
            return {}

        # Return cached data if still fresh
        now = time.time()
        if (
            self._villager_cache
            and self._villager_cache_time
            and now - self._villager_cache_time < self._villager_cache_ttl
        ):
            return self._villager_cache

        data = {}

        try:
            for base_dir in paths_to_scan:
                for root, dirs, files in os.walk(base_dir):
                    if "Villagers.txt" in files:
                        location_name = os.path.basename(root)
                        file_path = os.path.join(root, "Villagers.txt")

                        try:
                            with open(file_path, 'rb') as file:
                                raw_content = file.read().decode('utf-8', errors='ignore')

                                # Clean content
                                raw_content = re.sub(r'Villagers\s+on\s+[^:]+:', '', raw_content, flags=re.IGNORECASE)
                                names_list = re.split(r'[,\n\r]+', raw_content)

                                for name in names_list:
                                    clean_name = name.strip()

                                    if not clean_name or len(clean_name) > 30:
                                        continue

                                    # Handle special cases
                                    if clean_name in ["Ren?E", "Ren?e"]:
                                        clean_name = "Renée"

                                    key = clean_name.lower()

                                    if key in data:
                                        current_locs = data[key].split(", ")
                                        if location_name not in current_locs:
                                            data[key] += f", {location_name}"
                                    else:
                                        data[key] = location_name

                        except Exception as file_err:
                            logger.error(f"Error reading villagers file at {location_name}: {file_err}")

            self._villager_cache = data
            self._villager_cache_time = now
            return data

        except Exception as e:
            logger.error(f"Villager scan failed: {e}")
            return self._villager_cache or {}