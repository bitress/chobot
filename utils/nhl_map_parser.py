"""
Island map data module (live SysBot API) plus item/recipe catalog search

Parses Animal Crossing: New Horizons island map layers fetched live from the SysBot API
(GET /api/island/map/data). No .nhl files are read from disk.
"""

import os
import json
import logging
import time
from typing import Dict, List, Optional, Any

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


def get_island_map_data(island_name: str, force_refresh: bool = False) -> Dict[str, Any]:
    """
    Retrieve map data for an island from the live SysBot API (GET /api/island/map/data).
    No .nhl files are read from disk. Results are cached briefly per island.
    """
    clean_name = island_name.strip()
    norm_key = clean_name.lower()

    if not force_refresh:
        cached_entry = _MAP_CACHE.get(norm_key)
        if cached_entry and time.time() - cached_entry.get("cached_at", 0) < _CACHE_TTL_SECONDS:
            return cached_entry["data"]

    empty = {
        "ok": False,
        "island": clean_name,
        "status": "offline",
        "file_found": False,
        "file_path": None,
        "grid": {
            "width": 224,
            "height": 192,
            "acre_size": 32,
            "cols": ["A", "B", "C", "D", "E", "F", "G"],
            "rows": [1, 2, 3, 4, 5, 6],
        },
        "items": [],
        "sectors": {},
        "sector_summary": {},
        "stats": {"total_items": 0, "unique_categories": 0, "category_counts": {}},
    }

    try:
        from utils.sysbot_api import SysBotClientPool
        client = SysBotClientPool.get_client(clean_name)
        raw = client.get_island_map_data_sync() if client and client.base_url else None
    except Exception as exc:
        logger.debug(f"[NhlMapParser] SysBot map fetch failed for {clean_name}: {exc}")
        raw = None

    if not isinstance(raw, dict) or raw.get("dots") is None or raw.get("names") is None:
        return {**empty, "error": f"SysBot map data unavailable for island '{clean_name}'."}

    data = convert_sysbot_map_data(raw, clean_name)
    _MAP_CACHE[norm_key] = {"cached_at": time.time(), "data": data}
    return data


def convert_sysbot_map_data(raw_data: dict, island_name: str) -> dict:
    """
    Transforms SysBot HttpApiServer.cs compact map data (dots & names array)
    into the full website schema expected by InteractiveIslandMapModal.
    """
    cols = ["A", "B", "C", "D", "E", "F", "G"]
    rows = [1, 2, 3, 4, 5, 6]
    gw = int(raw_data.get("gw") or 224)
    gh = int(raw_data.get("gh") or 192)
    names = raw_data.get("names") or []
    dots = raw_data.get("dots") or []

    items = []
    sectors: dict = {}
    category_counts: dict = {}

    for dot in dots:
        if not isinstance(dot, (list, tuple)) or len(dot) < 7:
            continue
        x, y, name_idx, count, is_diy, item_id_hex, category = dot[:7]
        name = names[name_idx] if 0 <= name_idx < len(names) else f"Item #{item_id_hex}"
        col_idx = min(6, max(0, int(x) // 32))
        row_idx = min(5, max(0, int(y) // 32))
        sector = f"{cols[col_idx]}{rows[row_idx]}"

        cat_str = str(category or "other").lower()
        category_counts[cat_str] = category_counts.get(cat_str, 0) + 1

        img_url = ""

        item_obj = {
            "name": name,
            "category": cat_str,
            "diy": bool(is_diy),
            "itemIdHex": str(item_id_hex),
            "x": int(x),
            "y": int(y),
            "sector": sector,
            "count": int(count) if int(count) > 1 else 1,
            "record_index": int(x) * gh + int(y),
            "imageUrl": img_url,
            "source": "HttpApiServer.cs",
        }
        items.append(item_obj)
        sectors.setdefault(sector, []).append(item_obj)

    sector_summary = {}
    for sec, sec_items in sectors.items():
        sec_cat_counts: dict = {}
        for it in sec_items:
            c = it["category"]
            sec_cat_counts[c] = sec_cat_counts.get(c, 0) + 1
        top_cats = sorted(sec_cat_counts.items(), key=lambda kv: kv[1], reverse=True)[:3]
        sector_summary[sec] = {
            "total_items": len(sec_items),
            "top_categories": [cat for cat, _ in top_cats],
            "sample_items": [i["name"] for i in sec_items[:5]],
        }

    return {
        "ok": True,
        "island": island_name,
        "status": "live_ram",
        "file_found": True,
        "source": "HttpApiServer.cs",
        "grid": {
            "width": gw,
            "height": gh,
            "acre_size": 32,
            "cols": cols,
            "rows": rows,
        },
        "stats": {
            "total_items": len(items),
            "unique_categories": len(category_counts),
            "category_counts": category_counts,
        },
        "sector_summary": sector_summary,
        "sectors": sectors,
        "items": items,
        "names": names,
        "dots": dots,
        "paths": raw_data.get("paths") or [],
        "players": raw_data.get("players") or [],
        "generated_at": raw_data.get("generated_at"),
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
        islands_to_search = list(getattr(Config, "ALL_ISLAND_PORT_ORDER", []))

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
    """Return map availability across all configured ports, based on the SysBot map cache."""
    all_ports = getattr(Config, "ALL_ISLAND_PORT_ORDER", [])
    result = {}
    maps_found = 0
    total_items = 0

    for port_name in all_ports:
        clean = port_name.strip()
        cached = _MAP_CACHE.get(clean.lower())
        data = cached["data"] if cached else None
        has_map = bool(data and data.get("ok"))
        item_cnt = len(data.get("items", [])) if has_map else 0
        if has_map:
            maps_found += 1
            total_items += item_cnt
        result[clean] = {
            "has_map": has_map,
            "source": "sysbot_api",
            "cached_at": cached.get("cached_at") if cached else None,
            "cached_items_count": item_cnt,
        }

    return {
        "total_islands_checked": len(all_ports),
        "islands_with_maps": maps_found,
        "total_cached_items": total_items,
        "islands": result,
    }
