import json
import logging
import os
import re
from typing import Any, Dict, Optional

import requests

from utils.config import Config

logger = logging.getLogger("NookipediaClient")

VILLAGERS_JSON_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "villagers.json"
)

ZODIAC = [
    (1, 20, "Capricorn"), (2, 19, "Aquarius"), (3, 21, "Pisces"), (4, 20, "Aries"),
    (5, 21, "Taurus"), (6, 21, "Gemini"), (7, 23, "Cancer"), (8, 23, "Leo"),
    (9, 23, "Virgo"), (10, 23, "Libra"), (11, 22, "Scorpio"), (12, 22, "Sagittarius"),
    (12, 32, "Capricorn")
]

MONTHS = {
    1: "January", 2: "February", 3: "March", 4: "April", 5: "May", 6: "June",
    7: "July", 8: "August", 9: "September", 10: "October", 11: "November", 12: "December"
}


def _get_sign_and_bday(bday_str: str) -> tuple[str, str, str]:
    """Parse month, day, and zodiac sign from birthday string (e.g. '8/11' or 'August 11')."""
    if not bday_str:
        return "", "", "Unknown"
    if "/" in bday_str:
        parts = bday_str.split("/")
        try:
            m, d = int(parts[0]), int(parts[1])
            m_name = MONTHS.get(m, "")
            d_name = str(d)
            sign = "Unknown"
            for end_m, end_d, s in ZODIAC:
                if m < end_m or (m == end_m and d <= end_d):
                    sign = s
                    break
            return m_name, d_name, sign
        except Exception:
            return "", "", "Unknown"
    return "", "", "Unknown"


class NookipediaClient:
    BASE_URL = "https://api.nookipedia.com/villagers"
    FALLBACK_DATASET_URL = "https://raw.githubusercontent.com/Norviah/animal-crossing/master/json/data/Villagers.json"

    # In-memory dictionary of all villager data loaded from villagers.json
    _cache: Dict[str, Dict[str, Any]] = {}
    _is_loaded: bool = False

    @staticmethod
    def _normalize_name(name: str) -> str:
        """Normalize villager name for robust lookup."""
        if not name:
            return ""
        s = name.strip().lower()
        s = re.sub(r"[^\w\s]", " ", s)
        return re.sub(r"\s+", " ", s).strip()

    @classmethod
    def _index_villager(cls, key: str, villager: Dict[str, Any]) -> None:
        """Index a villager under multiple normalized variations."""
        raw = key.strip().lower()
        if raw:
            cls._cache[raw] = villager
        norm = cls._normalize_name(key)
        if norm:
            cls._cache[norm] = villager
        compact = re.sub(r"[^\w]", "", key.lower())
        if compact:
            cls._cache[compact] = villager

    @classmethod
    def _ensure_loaded(cls) -> None:
        """Load villagers.json into memory if exists, or download once on initial start."""
        if cls._is_loaded and cls._cache:
            return

        if os.path.exists(VILLAGERS_JSON_PATH):
            try:
                with open(VILLAGERS_JSON_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)

                cls._cache = {}
                if isinstance(data, dict):
                    for k, v in data.items():
                        cls._index_villager(k, v)
                        if v.get("name"):
                            cls._index_villager(v["name"], v)
                elif isinstance(data, list):
                    for item in data:
                        name = item.get("name", "")
                        if name:
                            cls._index_villager(name, item)

                cls._is_loaded = True
                logger.info(f"[NOOKIPEDIA] Loaded {len(cls._cache)} villager keys from local {VILLAGERS_JSON_PATH}")
                return
            except Exception as e:
                logger.error(f"[NOOKIPEDIA] Failed to read {VILLAGERS_JSON_PATH}: {e}")

        # If file does not exist, download on first start and save
        logger.info(f"[NOOKIPEDIA] {VILLAGERS_JSON_PATH} not found. Downloading villager data on initial start...")
        cls._download_and_save_data()

    @classmethod
    def _download_and_save_data(cls) -> None:
        """Download villager data from Nookipedia or fallback dataset and save to villagers.json."""
        villagers_dict: Dict[str, Dict[str, Any]] = {}

        # 1. Try Nookipedia API first if key exists
        if Config.NOOKIPEDIA_KEY:
            try:
                headers = {
                    "X-API-KEY": Config.NOOKIPEDIA_KEY,
                    "Accept-Version": "1.0.0"
                }
                params = {"nhdetails": "true"}
                resp = requests.get(cls.BASE_URL, headers=headers, params=params, timeout=15)
                if resp.status_code == 200:
                    raw_data = resp.json()
                    if isinstance(raw_data, list) and raw_data:
                        for v in raw_data:
                            name = v.get("name", "").strip()
                            if name:
                                villagers_dict[name.lower()] = v
                        logger.info(f"[NOOKIPEDIA] Downloaded {len(villagers_dict)} villagers from Nookipedia API.")
            except Exception as e:
                logger.warning(f"[NOOKIPEDIA] API download failed, falling back to community dataset: {e}")

        # 2. If Nookipedia API returned nothing or key missing/failed, fetch complete ACNH dataset
        if not villagers_dict:
            try:
                acnh_images: Dict[str, str] = {}
                acnh_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "acnh.json")
                if os.path.exists(acnh_path):
                    with open(acnh_path, "r", encoding="utf-8") as f:
                        acnh = json.load(f)
                    for it in acnh.get("villagers", {}).get("images", []):
                        if it.get("name") and it.get("url"):
                            acnh_images[it["name"].strip().lower()] = it["url"]

                r = requests.get(cls.FALLBACK_DATASET_URL, timeout=20)
                if r.status_code == 200:
                    raw_villagers = r.json()
                    for v in raw_villagers:
                        name = v.get("name", "").strip()
                        if not name:
                            continue
                        key = name.lower()
                        m_name, d_name, sign = _get_sign_and_bday(v.get("birthday", ""))
                        img_url = acnh_images.get(key) or v.get("photoImage") or v.get("iconImage") or ""

                        villager_entry = {
                            "id": v.get("filename") or v.get("uniqueEntryId") or "",
                            "name": name,
                            "url": "https://nookipedia.com/wiki/" + name.replace(" ", "_"),
                            "alt_name": "",
                            "title_color": v.get("bubbleColor") or "",
                            "text_color": v.get("nameColor") or "",
                            "species": v.get("species") or "Unknown",
                            "personality": v.get("personality") or "Unknown",
                            "gender": v.get("gender") or "Unknown",
                            "birthday_month": m_name,
                            "birthday_day": d_name,
                            "sign": sign,
                            "quote": v.get("favoriteSaying") or "",
                            "phrase": v.get("catchphrase") or "",
                            "clothing": v.get("defaultClothing") or "",
                            "islander": False,
                            "image_url": img_url,
                            "nh_details": {
                                "image_url": img_url,
                                "photo_url": v.get("photoImage") or "",
                                "icon_url": v.get("iconImage") or "",
                                "quote": v.get("favoriteSaying") or "",
                                "sub_personality": v.get("subtype") or "",
                                "catchphrase": v.get("catchphrase") or "",
                                "clothing": v.get("defaultClothing") or "",
                                "clothing_variation": "",
                                "fav_styles": v.get("styles") or [],
                                "fav_colors": v.get("colors") or [],
                                "hobby": v.get("hobby") or "Unknown",
                                "house_interior_url": v.get("houseImage") or "",
                                "house_exterior_url": "",
                                "house_wallpaper": v.get("wallpaper") or "Unknown",
                                "house_flooring": v.get("flooring") or "Unknown",
                                "house_music": v.get("favoriteSong") or "Unknown",
                                "house_music_note": "",
                                "umbrella": v.get("defaultUmbrella") or "",
                            }
                        }
                        villagers_dict[key] = villager_entry
                    logger.info(f"[NOOKIPEDIA] Downloaded {len(villagers_dict)} villagers from dataset.")
            except Exception as e:
                logger.error(f"[NOOKIPEDIA] Failed to download fallback dataset: {e}")

        # 3. Save to villagers.json once and populate cache
        if villagers_dict:
            try:
                with open(VILLAGERS_JSON_PATH, "w", encoding="utf-8") as f:
                    json.dump(villagers_dict, f, indent=2, ensure_ascii=False)
                logger.info(f"[NOOKIPEDIA] Saved {len(villagers_dict)} villagers to {VILLAGERS_JSON_PATH}")
            except Exception as e:
                logger.error(f"[NOOKIPEDIA] Failed to write {VILLAGERS_JSON_PATH}: {e}")

            cls._cache = {}
            for k, v in villagers_dict.items():
                norm_k = cls._normalize_name(k)
                if norm_k:
                    cls._cache[norm_k] = v
                raw_k = k.strip().lower()
                if raw_k and raw_k not in cls._cache:
                    cls._cache[raw_k] = v
            cls._is_loaded = True

    @classmethod
    async def get_villager_info(cls, name: str) -> Optional[Dict[str, Any]]:
        """Fetch villager data asynchronously from local villagers.json cache."""
        return cls.get_villager_info_sync(name)

    @classmethod
    def get_villager_info_sync(cls, name: str) -> Optional[Dict[str, Any]]:
        """Fetch villager data synchronously from local villagers.json cache."""
        if not name:
            return None

        cls._ensure_loaded()

        raw_key = name.strip().lower()
        if raw_key in cls._cache:
            return cls._cache[raw_key]

        norm_key = cls._normalize_name(name)
        if norm_key in cls._cache:
            return cls._cache[norm_key]

        return None


# Eagerly load the local villagers.json cache on module load
NookipediaClient._ensure_loaded()
