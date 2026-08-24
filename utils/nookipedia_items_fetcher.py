"""
Nookipedia Items API Downloader & Cache Generator
Fetches official item datasets directly from https://api.nookipedia.com/nh/*
and caches them into items_nookipedia.json.
"""

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional
import requests

from utils.config import Config

logger = logging.getLogger("NookipediaItemsFetcher")

NOOKIPEDIA_ITEMS_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "items_nookipedia.json"
)

# Official Nookipedia ACNH Item endpoints
ENDPOINTS = [
    ("items", "https://api.nookipedia.com/nh/items"),
    ("furniture", "https://api.nookipedia.com/nh/furniture"),
    ("clothing", "https://api.nookipedia.com/nh/clothing"),
    ("interior", "https://api.nookipedia.com/nh/interior"),
    ("tools", "https://api.nookipedia.com/nh/tools"),
    ("recipes", "https://api.nookipedia.com/nh/recipes"),
    ("art", "https://api.nookipedia.com/nh/art"),
    ("fossils", "https://api.nookipedia.com/nh/fossils/all"),
    ("fish", "https://api.nookipedia.com/nh/fish"),
    ("bugs", "https://api.nookipedia.com/nh/bugs"),
    ("sea", "https://api.nookipedia.com/nh/sea"),
]


def fetch_and_cache_all_nookipedia_items(api_key: Optional[str] = None) -> Dict[str, Any]:
    """
    Fetch complete item datasets from official Nookipedia API and save to items_nookipedia.json.
    """
    key = api_key or Config.NOOKIPEDIA_KEY
    if not key:
        raise ValueError("A valid Nookipedia API key is required (get one at https://nookipedia.com/api-keys/)")

    headers = {
        "X-API-KEY": key.strip(),
        "Accept-Version": "1.0.0"
    }

    all_items: Dict[str, Dict[str, Any]] = {}
    stats: Dict[str, int] = {}

    print(f"Connecting to Nookipedia API (Key: {key[:8]}...)...")

    for cat_name, url in ENDPOINTS:
        try:
            print(f"Fetching {cat_name} from {url}...")
            resp = requests.get(url, headers=headers, timeout=25)

            if resp.status_code == 401:
                raise PermissionError(
                    f"HTTP 401 Unauthorized: Nookipedia rejected key '{key}'. "
                    "Please generate a valid key at https://nookipedia.com/api-keys/ and add to .env"
                )

            if resp.status_code != 200:
                print(f"Warning: {cat_name} returned status {resp.status_code}: {resp.text[:100]}")
                continue

            data = resp.json()
            items_list = data if isinstance(data, list) else [data]
            count = 0

            for it in items_list:
                name = it.get("name", "").strip()
                if not name:
                    continue

                it["nookipedia_category"] = cat_name
                all_items[name.lower()] = it
                count += 1

            stats[cat_name] = count
            print(f"✓ {cat_name}: {count} items fetched.")
            time.sleep(0.5)  # Respect rate limits

        except PermissionError:
            raise
        except Exception as exc:
            print(f"Error fetching {cat_name}: {exc}")

    if all_items:
        with open(NOOKIPEDIA_ITEMS_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(all_items, f, indent=2, ensure_ascii=False)
        print(f"\nSuccessfully cached {len(all_items)} items to {NOOKIPEDIA_ITEMS_CACHE_PATH}")

    return all_items


if __name__ == "__main__":
    import sys
    custom_key = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        fetch_and_cache_all_nookipedia_items(custom_key)
    except Exception as e:
        print(f"\n[ERROR] {e}")
