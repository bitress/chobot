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

        self._connect_sheets()
        self.load_image_catalog()

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
                    time.sleep(2.0)

                except Exception as e:
                    logger.error(f"Error reading '{sheet.title}': {e}")

            temp_cache["_display"] = display_map

            with self.lock:
                self.cache = temp_cache
                self.last_update = datetime.now()

            logger.info(f"Scan complete. {len(temp_cache)} items loaded from {sheets_scanned} sheets.")

        except Exception as e:
            logger.error(f"Workbook fetch failed: {e}")

    def auto_refresh_loop(self):
        """Background thread to auto-refresh cache"""
        while True:
            time.sleep(3600 * self.cache_refresh_hours)
            self.update_cache()

    def get_villagers(self, villagers_dirs):
        """Scan villager text files from provided directories

        Args:
            villagers_dirs: List of directory paths to scan
        """
        data = {}
        paths_to_scan = [p for p in villagers_dirs if p and os.path.exists(p)]

        if not paths_to_scan:
            return data

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

            return data

        except Exception as e:
            logger.error(f"Villager scan failed: {e}")
            return {}