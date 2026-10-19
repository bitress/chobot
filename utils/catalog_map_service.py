"""
Catalog, Interactive Island Map & Live Telemetry Service
========================================================
Provides high-performance search and rendering utilities for:
- Item search & details (/api/items/search)
- DIY Recipe crafting & ingredient search (/api/recipes/search)
- Item customizations & variations (/api/items/customizations)
- Unified island item locator (/api/island/items)
- Interactive island radar map generator & scaler (/api/island/map/image)
- Multi-island telemetry & flight logger summary (/api/telemetry)
- Live players & island visitor tracker (/api/players/live)
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

from utils.config import Config
from utils.database import connect_db

logger = logging.getLogger("CatalogMapService")

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MAP_STORAGE_DIR = os.path.join(_BASE_DIR, "storage", "maps")
os.makedirs(_MAP_STORAGE_DIR, exist_ok=True)

# In-memory cached indices
_LOCK = threading.RLock()
_ITEMS_LOADED = False
_ITEMS_LIST: List[Dict[str, Any]] = []
_ITEMS_BY_NAME: Dict[str, Dict[str, Any]] = {}
_ITEMS_BY_ID: Dict[str, Dict[str, Any]] = {}
_RECIPES_LIST: List[Dict[str, Any]] = []
_TREASURE_ISLANDS: List[Dict[str, Any]] = []
_TREASURE_BY_ITEM: Dict[str, List[Dict[str, Any]]] = {}
_TREASURE_BY_ISLAND: Dict[str, List[Dict[str, Any]]] = {}


def _ensure_catalog_loaded() -> None:
    """Ensure catalog and recipes are loaded into memory once for sub-millisecond responses."""
    global _ITEMS_LOADED, _ITEMS_LIST, _ITEMS_BY_NAME, _ITEMS_BY_ID
    global _RECIPES_LIST, _TREASURE_ISLANDS, _TREASURE_BY_ITEM, _TREASURE_BY_ISLAND

    if _ITEMS_LOADED:
        return

    with _LOCK:
        if _ITEMS_LOADED:
            return

        t0 = time.time()
        # 1. Load items_detail.json
        items_detail_file = os.path.join(_BASE_DIR, "items_detail.json")
        if not os.path.exists(items_detail_file):
            items_detail_file = os.path.join(_BASE_DIR, "explorer.json")

        if os.path.exists(items_detail_file):
            try:
                with open(items_detail_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    _ITEMS_LIST = data.get("items", [])
                    _TREASURE_ISLANDS = data.get("treasureIslands", [])

                    for it in _ITEMS_LIST:
                        name = (it.get("Name") or "").strip()
                        if name:
                            _ITEMS_BY_NAME[name.lower()] = it
                        internal_id = str(it.get("Internal ID") or "").strip()
                        if internal_id:
                            _ITEMS_BY_ID[internal_id.lower()] = it

                    for ti in _TREASURE_ISLANDS:
                        ti_item = (ti.get("item") or "").strip().lower()
                        ti_isl = (ti.get("islandID") or "").strip().lower()
                        if ti_item:
                            _TREASURE_BY_ITEM.setdefault(ti_item, []).append(ti)
                        if ti_isl:
                            _TREASURE_BY_ISLAND.setdefault(ti_isl, []).append(ti)
            except Exception as exc:
                logger.error(f"[CatalogMapService] Error loading items_detail.json: {exc}")

        # 2. Load recipes from data/acnh.min.json
        acnh_min_file = os.path.join(_BASE_DIR, "data", "acnh.min.json")
        if os.path.exists(acnh_min_file):
            try:
                with open(acnh_min_file, "r", encoding="utf-8") as f:
                    acnh_data = json.load(f)
                    _RECIPES_LIST = acnh_data.get("recipes", [])
            except Exception as exc:
                logger.error(f"[CatalogMapService] Error loading recipes from acnh.min.json: {exc}")

        _ITEMS_LOADED = True
        logger.info(
            f"[CatalogMapService] Loaded catalog in {time.time() - t0:.2f}s: "
            f"{len(_ITEMS_LIST)} items, {len(_RECIPES_LIST)} recipes, {len(_TREASURE_ISLANDS)} island items"
        )


# ============================================================================
# 1. ITEM SEARCH
# ============================================================================

def search_catalog_items(
    query: str,
    category: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> Dict[str, Any]:
    """Search items by name, category, or internal ID with ranking."""
    _ensure_catalog_loaded()
    q = (query or "").strip().lower()
    cat_filter = (category or "").strip().lower()

    if not q and not cat_filter:
        subset = _ITEMS_LIST[offset : offset + limit]
        return {
            "ok": True,
            "query": query,
            "total": len(_ITEMS_LIST),
            "limit": limit,
            "offset": offset,
            "items": [_format_item_summary(it) for it in subset],
        }

    exact_matches: List[Dict[str, Any]] = []
    prefix_matches: List[Dict[str, Any]] = []
    contains_matches: List[Dict[str, Any]] = []

    for it in _ITEMS_LIST:
        it_cat = (it.get("Category") or "").lower()
        if cat_filter and cat_filter not in it_cat:
            continue

        name = (it.get("Name") or "").lower()
        internal_id = str(it.get("Internal ID") or "").lower()

        if q:
            if name == q or internal_id == q:
                exact_matches.append(it)
            elif name.startswith(q):
                prefix_matches.append(it)
            elif q in name or q in internal_id or q in (it.get("Series") or "").lower():
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
        "items": [_format_item_summary(it) for it in paginated],
    }


def _format_item_summary(item: Dict[str, Any]) -> Dict[str, Any]:
    """Format an item dictionary into a clean, modern API representation."""
    variations = item.get("Variations") or []
    first_var = variations[0] if variations else {}
    img_url = first_var.get("imageUrl") or ""

    diy_val = item.get("DIY")
    is_diy = str(diy_val).lower() in ("yes", "true", "1") if diy_val is not None else False

    return {
        "name": item.get("Name") or "",
        "category": item.get("Category") or "",
        "internal_id": item.get("Internal ID") or "",
        "buy": item.get("Buy") or "NA",
        "sell": item.get("Sell") or "NA",
        "source": item.get("Source") or "",
        "series": item.get("Series") or "",
        "diy": is_diy,
        "customizable": len(variations) > 1 or bool(item.get("BodyTitle") or item.get("PatternTitle")),
        "variations_count": len(variations),
        "colors": item.get("Colours") or [],
        "image_url": img_url,
        "body_title": item.get("BodyTitle"),
        "pattern_title": item.get("PatternTitle"),
        "variations": variations,
    }


# ============================================================================
# 2. RECIPE SEARCH
# ============================================================================

def search_catalog_recipes(
    query: str,
    category: Optional[str] = None,
    material: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> Dict[str, Any]:
    """Search crafting DIY recipes by recipe name, crafted item, or material requirement."""
    _ensure_catalog_loaded()
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

        matches.append({
            "name": r.get("name") or "",
            "category": r.get("category") or "",
            "image": r.get("image") or "",
            "buy": r.get("buy"),
            "sell": r.get("sell"),
            "source": r.get("source") or [],
            "season_event": r.get("seasonEvent"),
            "internal_id": r.get("internalId"),
            "crafted_item_internal_id": r.get("craftedItemInternalId"),
            "materials": r.get("materials") or {},
            "unique_entry_id": r.get("uniqueEntryId") or "",
        })

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


# ============================================================================
# 3. ITEM CUSTOMIZATIONS
# ============================================================================

def get_item_customizations(item_query: str) -> Dict[str, Any]:
    """Retrieve full customization kits, Cyrus price, and variations for an item."""
    _ensure_catalog_loaded()
    q = (item_query or "").strip().lower()
    if not q:
        return {"ok": False, "error": "Item query is required", "found": False}

    target = _ITEMS_BY_NAME.get(q) or _ITEMS_BY_ID.get(q)

    # Fuzzy match if exact lookup fails
    if not target:
        for it in _ITEMS_LIST:
            if q in (it.get("Name") or "").lower():
                target = it
                break

    if not target:
        return {"ok": True, "found": False, "item": item_query, "variations": []}

    variations_raw = target.get("Variations") or []
    variations = []
    for v in variations_raw:
        variations.append({
            "variation": v.get("Variation") or v.get("variation") or "Standard",
            "pattern": v.get("Pattern") or v.get("pattern"),
            "id": v.get("id"),
            "poker_id": v.get("pokerId"),
            "filename": v.get("Filename"),
            "image_url": v.get("imageUrl") or v.get("storageImage") or v.get("closetImage") or "",
            "colors": v.get("Colours") or [],
            "unique_entry_id": v.get("uniqueEntryId"),
        })

    is_customizable = len(variations) > 1 or bool(target.get("BodyTitle") or target.get("PatternTitle"))

    return {
        "ok": True,
        "found": True,
        "item": target.get("Name"),
        "category": target.get("Category"),
        "internal_id": target.get("Internal ID"),
        "customizable": is_customizable,
        "body_title": target.get("BodyTitle"),
        "pattern_title": target.get("PatternTitle"),
        "total_variations": len(variations),
        "variations": variations,
    }


# ============================================================================
# 4. UNIFIED ISLAND ITEMS (Interactive Map item finder)
# ============================================================================

def search_island_items_unified(
    query: str,
    island_name: Optional[str] = None,
    acre: Optional[str] = None,
    category: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> Dict[str, Any]:
    """
    Search on-ground items across treasure islands and sub islands.
    Integrates:
    - Real .nhl parsed live map items (utils.nhl_map_parser)
    - Explorer / treasure islands database (items_detail.json)
    - In-memory item cache location mapping (cache_dump.json)
    """
    _ensure_catalog_loaded()
    q = (query or "").strip().lower()
    target_isl = (island_name or "").strip()
    acre_filter = (acre or "").strip().lower()
    cat_filter = (category or "").strip().lower()

    results: List[Dict[str, Any]] = []
    seen_keys: set[tuple[str, str, str]] = set()

    # 1. Try NHL live binary map parser if a specific island is requested
    if target_isl and target_isl.lower() != "all":
        try:
            from utils.nhl_map_parser import search_island_items
            nhl_res = search_island_items(target_isl, q)
            if nhl_res.get("ok") and nhl_res.get("matches"):
                for m in nhl_res["matches"]:
                    m_name = m.get("name") or ""
                    m_acre = m.get("acre") or m.get("sector") or ""
                    key = (m_name.lower(), target_isl.lower(), m_acre.lower())
                    if key not in seen_keys:
                        seen_keys.add(key)
                        results.append({
                            "name": m_name,
                            "island": target_isl,
                            "acre": m.get("acre") or "",
                            "sector": m.get("sector") or "",
                            "x": m.get("x"),
                            "y": m.get("y"),
                            "category": m.get("category") or "On Ground",
                            "item_id": m.get("item_id"),
                            "source": "live_nhl_map",
                        })
        except Exception as exc:
            logger.debug(f"[CatalogMapService] NHL search note: {exc}")

    # 2. Search treasure islands database from items_detail.json
    ti_candidates = _TREASURE_ISLANDS
    if target_isl and target_isl.lower() != "all":
        ti_candidates = _TREASURE_BY_ISLAND.get(target_isl.lower(), [])
        if not ti_candidates:
            # Fallback scan in case of slight casing mismatch
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
            results.append({
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

    # 3. Search item cache locations (GSheets / cache_dump.json)
    if not results and q:
        try:
            cache_file = os.path.join(_BASE_DIR, "cache_dump.json")
            if os.path.exists(cache_file):
                with open(cache_file, "r", encoding="utf-8") as f:
                    cdata = json.load(f)
                    for k, loc_str in cdata.items():
                        if k.startswith("_"):
                            continue
                        if q in k.lower():
                            islands_found = [s.strip() for s in str(loc_str).split(",") if s.strip()]
                            for isl in islands_found:
                                if target_isl and target_isl.lower() != "all" and target_isl.lower() != isl.lower():
                                    continue
                                key = (k.lower(), isl.lower(), "")
                                if key not in seen_keys:
                                    seen_keys.add(key)
                                    results.append({
                                        "name": k,
                                        "island": isl,
                                        "acre": "Island Wide",
                                        "sector": "",
                                        "x": None,
                                        "y": None,
                                        "category": "Available",
                                        "item_id": None,
                                        "source": "island_item_cache",
                                    })
        except Exception as exc:
            logger.debug(f"[CatalogMapService] Cache search note: {exc}")

    total = len(results)
    paginated = results[offset : offset + limit]

    return {
        "ok": True,
        "query": query,
        "island": target_isl or "all",
        "acre": acre,
        "category": category,
        "total_matches": total,
        "limit": limit,
        "offset": offset,
        "items": paginated,
    }


# ============================================================================
# 5. ISLAND RADAR MAP IMAGE RENDERER & SCALER
# ============================================================================

def render_island_map_image(
    island_name: str = "Hiraya",
    scale: float = 1.0,
    theme: str = "radar",
    overlay_items: bool = False,
    overlay_players: bool = True,
    format_type: str = "png",
) -> Tuple[bytes, str]:
    """
    Renders or scales an island map image:
    1. Looks for cached CDN map at storage/maps/<island>.png or fetches from https://cdn.chopaeng.com/maps/<island>.png.
    2. If not on CDN, generates a high-definition ACNH 7x6 acre radar map with coordinates, landmass, and ocean.
    3. Overlays live visitor radar blips if overlay_players is true.
    4. Resizes smoothly using scale (clamped 0.1 .. 4.0).
    5. Returns (image_bytes, mimetype).
    """
    clean_name = (island_name or "Hiraya").strip()
    norm_name = clean_name.lower()
    local_cache_path = os.path.join(_MAP_STORAGE_DIR, f"{norm_name}.png")

    img: Optional[Image.Image] = None

    # Step 1: Check local storage cache
    if os.path.exists(local_cache_path):
        try:
            img = Image.open(local_cache_path).convert("RGBA")
        except Exception:
            img = None

    # Step 2: If not in local storage, attempt to fetch from CDN
    if img is None:
        cdn_url = f"https://cdn.chopaeng.com/maps/{norm_name}.png"
        try:
            req = urllib.request.Request(cdn_url, headers={"User-Agent": "ChoBot-MapService/1.0"})
            with urllib.request.urlopen(req, timeout=3.5) as resp:
                if resp.status == 200:
                    raw_data = resp.read()
                    img = Image.open(io.BytesIO(raw_data)).convert("RGBA")
                    # Save to local storage for fast future access
                    with open(local_cache_path, "wb") as f:
                        f.write(raw_data)
        except Exception:
            img = None

    # Step 3: Procedural ACNH Radar Map Generation if CDN image is not present
    if img is None:
        img = _generate_procedural_radar_map(clean_name, theme=theme)

    # Step 4: Overlay live players if active
    if overlay_players:
        img = _overlay_live_players(img, clean_name)

    # Step 5: Scale image
    clamped_scale = max(0.1, min(4.0, float(scale)))
    if abs(clamped_scale - 1.0) > 0.01:
        new_w = max(32, int(img.width * clamped_scale))
        new_h = max(32, int(img.height * clamped_scale))
        img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

    # Export to requested image format
    buffer = io.BytesIO()
    fmt = format_type.strip().lower()
    if fmt in ("jpg", "jpeg"):
        img_rgb = img.convert("RGB")
        img_rgb.save(buffer, format="JPEG", quality=90)
        mimetype = "image/jpeg"
    elif fmt == "webp":
        img.save(buffer, format="WEBP", quality=90)
        mimetype = "image/webp"
    else:
        img.save(buffer, format="PNG", optimize=True)
        mimetype = "image/png"

    return buffer.getvalue(), mimetype


def _generate_procedural_radar_map(island_name: str, theme: str = "radar") -> Image.Image:
    """Generate a crisp, aesthetic Animal Crossing 7x6 Acre Radar Map."""
    width, height = 896, 768  # 112x96 tiles * 8px
    img = Image.new("RGBA", (width, height), (26, 68, 115, 255))  # Ocean blue
    draw = ImageDraw.Draw(img)

    # Shoreline beach (sand)
    draw.rounded_rectangle([48, 48, width - 48, height - 80], radius=44, fill=(235, 222, 160, 255))

    # Lush Grassland island terrain
    draw.rounded_rectangle([72, 72, width - 72, height - 104], radius=36, fill=(78, 175, 78, 255))

    # Airport South Pier
    pier_x1 = width // 2 - 36
    pier_x2 = width // 2 + 36
    draw.rectangle([pier_x1, height - 104, pier_x2, height - 32], fill=(160, 115, 75, 255), outline=(120, 85, 50, 255), width=3)

    # 7 Columns (A..G) x 6 Rows (1..6)
    col_width = width / 7.0
    row_height = height / 6.0
    col_letters = ["A", "B", "C", "D", "E", "F", "G"]

    # Subtle radar grid lines
    grid_color = (255, 255, 255, 60)
    for c in range(1, 7):
        cx = int(c * col_width)
        draw.line([(cx, 0), (cx, height)], fill=grid_color, width=1)
    for r in range(1, 6):
        ry = int(r * row_height)
        draw.line([(0, ry), (width, ry)], fill=grid_color, width=1)

    # Acre labels (e.g. A1, B2)
    for c_idx, letter in enumerate(col_letters):
        for r_idx in range(6):
            label = f"{letter}{r_idx + 1}"
            lx = int(c_idx * col_width + 8)
            ly = int(r_idx * row_height + 6)
            draw.text((lx, ly), label, fill=(255, 255, 255, 140))

    # Island Banner in top corner
    banner_w = 220
    draw.rounded_rectangle([16, 16, 16 + banner_w, 56], radius=10, fill=(15, 30, 50, 220), outline=(255, 215, 0, 200), width=2)
    draw.text((28, 24), f"ISLAND: {island_name.upper()}", fill=(255, 235, 120, 255))

    return img


def _overlay_live_players(img: Image.Image, island_name: str) -> Image.Image:
    """Overlay active visitor radar blips and name badges onto the island map."""
    try:
        from utils.sysbot_telemetry import get_cached_island_telemetry
        tel = get_cached_island_telemetry(island_name, mask_sub_dodo=True)
        if not tel:
            return img

        visitors = tel.get("visitor_list") or []
        if not visitors:
            return img

        draw = ImageDraw.Draw(img)
        # Position radar pings around central plazas
        col_width = img.width / 7.0
        row_height = img.height / 6.0

        center_x = img.width // 2
        center_y = int(row_height * 3.5)

        offsets = [
            (-60, -40), (60, -40), (-80, 50), (80, 50),
            (0, -90), (0, 90), (-120, 0), (120, 0)
        ]

        for idx, visitor in enumerate(visitors[:8]):
            dx, dy = offsets[idx % len(offsets)]
            px = center_x + dx
            py = center_y + dy

            # Glowing radar ring
            draw.ellipse([px - 14, py - 14, px + 14, py + 14], outline=(0, 255, 200, 180), width=3)
            # Dot
            draw.ellipse([px - 6, py - 6, px + 6, py + 6], fill=(255, 80, 80, 255))
            # Name tag badge
            v_name = str(visitor)[:12]
            name_box_w = max(40, len(v_name) * 8 + 12)
            draw.rounded_rectangle(
                [px - name_box_w // 2, py + 16, px + name_box_w // 2, py + 34],
                radius=6,
                fill=(10, 25, 45, 230),
                outline=(0, 255, 200, 200),
                width=1
            )
            draw.text((px - name_box_w // 2 + 6, py + 18), v_name, fill=(255, 255, 255, 255))

    except Exception as exc:
        logger.debug(f"[CatalogMapService] Overlay players note: {exc}")

    return img


# ============================================================================
# 6. TELEMETRY & HEALTH SUMMARY
# ============================================================================

def build_telemetry_payload(
    island_filter: Optional[str] = None,
    viewer: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Assemble high-speed telemetry across all 48 ports.
    Respects user authorization via Unified Auth for sub-island Dodo reveals.
    """
    from utils.sysbot_telemetry import (
        get_all_cached_telemetry,
        get_live_visitor_overview,
        get_all_islands_health,
        get_island_load_overview,
        _is_sub_island,
    )
    from utils import island_access

    all_ports = getattr(Config, "ALL_ISLAND_PORT_ORDER", [])
    raw_telemetry = get_all_cached_telemetry(mask_sub_dodo=False)

    viewer_roles = viewer.get("roles", []) if viewer else []
    viewer_is_admin = bool(viewer and viewer.get("is_admin"))
    viewer_is_mod = bool(viewer and (viewer.get("is_mod") or viewer_is_admin or island_access.is_mod(viewer_roles)))

    islands_result: Dict[str, Dict[str, Any]] = {}
    online_count = 0
    total_visitors = 0

    target_filter = (island_filter or "").strip().lower()

    for idx, raw_name in enumerate(all_ports):
        name_clean = raw_name.strip()
        norm_key = name_clean.lower()

        if target_filter and target_filter not in (norm_key, name_clean.upper()):
            continue

        tel_entry = raw_telemetry.get(norm_key, {})
        is_sub = _is_sub_island(name_clean)
        is_online = bool(tel_entry.get("is_online"))

        if is_online:
            online_count += 1

        v_list = tel_entry.get("visitor_list") or []
        v_count = tel_entry.get("visitors_count") if tel_entry.get("visitors_count") is not None else len(v_list)
        total_visitors += v_count

        # Check Dodo visibility based on unified auth
        can_see_dodo = False
        if not is_sub:
            can_see_dodo = True
        elif viewer_is_mod or viewer_is_admin:
            can_see_dodo = True
        elif viewer:
            can_see_dodo = island_access.has_island_access(
                viewer_roles,
                "member",
                name_clean,
                "VIP" if is_sub else "Free",
            )

        exposed_dodo = tel_entry.get("raw_dodo") if can_see_dodo else None

        gametime_info = tel_entry.get("gametime") or {}

        islands_result[name_clean.upper()] = {
            "name": name_clean,
            "port_index": idx + 1,
            "is_sub": is_sub,
            "type": "VIP" if is_sub else "Free",
            "is_online": is_online,
            "status": "ONLINE" if is_online else "OFFLINE",
            "dodo_code": exposed_dodo,
            "dodo_accessible": can_see_dodo,
            "visitors": v_list,
            "visitor_count": v_count,
            "max_visitors": 7,
            "gametime": gametime_info.get("formatted") or gametime_info.get("game_time"),
            "nooks_status": gametime_info.get("nooks_status"),
            "last_arrival": tel_entry.get("last_arrival"),
            "last_departure": tel_entry.get("last_departure"),
            "last_poll_ts": tel_entry.get("last_poll_ts"),
        }

    visitor_summary = get_live_visitor_overview()
    health_summary = get_all_islands_health()
    load_summary = get_island_load_overview()

    return {
        "ok": True,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total_islands": len(all_ports),
            "online_islands": online_count,
            "total_visitors": total_visitors,
            "active_islands_count": visitor_summary.get("active_islands_count", 0),
        },
        "visitor_overview": visitor_summary,
        "load_overview": load_summary,
        "health": health_summary,
        "islands": islands_result,
    }


# ============================================================================
# 7. LIVE PLAYERS TRACKER
# ============================================================================

def build_live_players_payload(
    island_filter: Optional[str] = None,
    viewer: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Retrieve real-time player presence across all islands.
    Cross-references SysBot active visitor lists with SQLite flight logs.
    """
    from utils.sysbot_telemetry import get_all_cached_telemetry

    all_telemetry = get_all_cached_telemetry(mask_sub_dodo=True)
    target_filter = (island_filter or "").strip().lower()

    # Pre-fetch recent arrival logs from DB for enrichment
    recent_arrivals: Dict[Tuple[str, str], Dict[str, Any]] = {}
    try:
        with connect_db() as conn:
            rows = conn.execute("""
                SELECT player, origin_island, island_name, timestamp
                FROM island_events_log
                WHERE event_type = 'arrival' AND player IS NOT NULL
                ORDER BY timestamp DESC LIMIT 200
            """).fetchall()
            for r in rows:
                p_name = str(r["player"]).strip().lower()
                isl = str(r["island_name"]).strip().lower()
                key = (isl, p_name)
                if key not in recent_arrivals:
                    recent_arrivals[key] = {
                        "origin_island": r["origin_island"] or "",
                        "arrived_at": r["timestamp"],
                    }
    except Exception as exc:
        logger.debug(f"[CatalogMapService] Arrivals enrichment note: {exc}")

    players_list: List[Dict[str, Any]] = []
    by_island: Dict[str, Dict[str, Any]] = {}

    total_online_islands = 0

    for isl_key, data in all_telemetry.items():
        isl_name = data.get("island") or isl_key.capitalize()
        norm_key = isl_key.lower()

        if target_filter and target_filter not in (norm_key, isl_name.lower()):
            continue

        if data.get("is_online"):
            total_online_islands += 1

        v_list = data.get("visitor_list") or []
        by_island[isl_name] = {
            "island": isl_name,
            "is_online": bool(data.get("is_online")),
            "visitor_count": len(v_list),
            "visitors": v_list,
        }

        for p in v_list:
            clean_p = str(p).strip()
            arr_info = recent_arrivals.get((norm_key, clean_p.lower()), {})
            players_list.append({
                "name": clean_p,
                "island": isl_name,
                "status": "visiting",
                "origin_island": arr_info.get("origin_island", ""),
                "arrived_at": arr_info.get("arrived_at"),
                "is_host": clean_p.lower() in ("chopaeng", "cho paeng"),
            })

    return {
        "ok": True,
        "total_players": len(players_list),
        "total_online_islands": total_online_islands,
        "active_islands_count": len([i for i in by_island.values() if i["visitor_count"] > 0]),
        "players": players_list,
        "by_island": by_island,
    }
