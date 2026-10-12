"""
SysBot Multi-Island Telemetry & Flight Logger Service.
Manages continuous background polling across 48 SysBot.ACNHOrders HTTP ports,
ingests switch-level flight activity events into SQLite, and provides
sub-millisecond cached telemetry for web requests—completely decoupled from Discord.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from utils.config import Config
from utils.database import connect_db
from utils.sysbot_api import get_island_sysbot_client

logger = logging.getLogger("SysBotTelemetry")

# Cache mapping island_name -> live state dict
_TELEMETRY_CACHE: Dict[str, Dict[str, Any]] = {}
_LAST_EVENT_IDS: Dict[str, int] = {}
_CACHE_LOCK = threading.Lock()

_WORKER_THREAD: Optional[threading.Thread] = None
_WORKER_STOP_EVENT = threading.Event()
_DB_INITIALIZED = False


def ensure_telemetry_schema() -> None:
    """Ensure SQLite tables for activity logs and villager injections exist."""
    global _DB_INITIALIZED
    if _DB_INITIALIZED:
        return

    with connect_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS island_events_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER,
                island_name VARCHAR(128) NOT NULL,
                event_type VARCHAR(64) NOT NULL,
                message TEXT NOT NULL,
                player VARCHAR(128),
                origin_island VARCHAR(128),
                dodo_code VARCHAR(16),
                network_id VARCHAR(64),
                timestamp DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
        """)
        try:
            conn.execute("CREATE INDEX IF NOT EXISTS ix_island_events_name_ts ON island_events_log(island_name, timestamp);")
        except Exception as exc:
            logger.debug("Telemetry index note (island_events_name_ts): %s", exc)

        try:
            conn.execute("CREATE INDEX IF NOT EXISTS ix_island_events_type ON island_events_log(event_type);")
        except Exception as exc:
            logger.debug("Telemetry index note (island_events_type): %s", exc)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS villager_injections_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                island_name VARCHAR(128) NOT NULL,
                villager_name VARCHAR(128) NOT NULL,
                plot_number INTEGER NOT NULL,
                requested_by_user_id VARCHAR(64),
                status VARCHAR(32) NOT NULL DEFAULT 'queued',
                response_message TEXT,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
        """)
        try:
            conn.execute("CREATE INDEX IF NOT EXISTS ix_villager_inj_island_ts ON villager_injections_log(island_name, created_at);")
        except Exception as exc:
            logger.debug("Telemetry index note (villager_inj_island_ts): %s", exc)

        # Ensure island_visits has destination and timestamp index
        try:
            conn.execute("CREATE INDEX IF NOT EXISTS ix_island_visits_dest_ts ON island_visits(destination, timestamp);")
        except Exception as exc:
            logger.debug("Telemetry index note (island_visits_dest_ts): %s", exc)

    _DB_INITIALIZED = True


def _is_sub_island(island_name: str) -> bool:
    """Check if an island is in the 20 Sub Island tier list."""
    clean = (island_name or "").strip().lower()
    canonical_sub = [
        "hiraya", "tagumpay", "bahaghari", "alapaap", "bituin",
        "lakan", "galak", "aruga", "likha", "dalisay",
        "marahuyo", "diwa", "bonita", "gabay", "dakila",
        "kalangitan", "malaya", "pangarap", "giliw", "adhika",
    ]
    return clean in canonical_sub


def parse_sysbot_visitors(status_data: Dict[str, Any]) -> tuple[int, list[str]]:
    """
    Extract clean visitor count and actual player visitor names from SysBot status payload.
    Correctly handles:
      - Explicit visitors_count integer (including 0!)
      - visitor_list (list of strings or raw visitors text)
      - Filters out header and '#N: Available slot'
    """
    raw_list = status_data.get("visitor_list")
    raw_str = status_data.get("visitors")

    lines: list[str] = []
    if isinstance(raw_list, list):
        lines = [str(l).strip() for l in raw_list if str(l).strip()]
    elif isinstance(raw_str, str):
        lines = [l.strip() for l in raw_str.strip().splitlines() if l.strip()]

    actual_names: list[str] = []
    for line in lines:
        if line.lower().startswith("the following visitors are on"):
            continue
        m = re.match(r"^#\d+:\s*(.+)$", line)
        if m:
            val = m.group(1).strip()
            if val.lower() != "available slot":
                actual_names.append(val)
        elif line and not line.lower().startswith("available slot"):
            actual_names.append(line)

    raw_count = status_data.get("visitors_count")
    if raw_count is not None:
        try:
            count = int(raw_count)
        except (ValueError, TypeError):
            count = len(actual_names)
    else:
        count = len(actual_names)

    return count, actual_names


def poll_single_island(island_name: str) -> Dict[str, Any]:
    """Poll a single island's SysBot HTTP port for status and new events."""
    client = get_island_sysbot_client(island_name, timeout=2)
    status_data = client.get_bot_status_sync()

    now_ts = time.time()
    clean_name = island_name.strip().title()
    is_sub = _is_sub_island(clean_name)

    is_online = bool(status_data.get("is_running") and status_data.get("is_connected"))
    dodo_code = status_data.get("dodo_code")
    visitors_count, visitor_list = parse_sysbot_visitors(status_data)

    cached_entry: Dict[str, Any] = {
        "island": clean_name,
        "is_online": is_online,
        "is_connected": bool(status_data.get("is_connected")),
        "console_stage": status_data.get("console_stage") or ("Running" if is_online else "Offline"),
        "mode": status_data.get("mode") or ("DropMode" if not is_sub else "DropMode"),
        "raw_dodo": dodo_code,  # Stored internally, hidden in public queries for sub islands
        "is_dodo_valid": bool(status_data.get("is_dodo_valid")),
        "is_refreshing_dodo": bool(status_data.get("is_refreshing_dodo")),
        "visitors_count": visitors_count,
        "visitor_list": visitor_list,
        "last_arrival": status_data.get("last_arrival"),
        "last_departure": status_data.get("last_departure"),
        "last_updated": now_ts,
    }

    with _CACHE_LOCK:
        _TELEMETRY_CACHE[clean_name.lower()] = cached_entry
        last_seen_id = _LAST_EVENT_IDS.get(clean_name.lower())

    # If island is online, fetch new activity events incrementally
    if is_online:
        events_resp = client.get_events_sync(since_id=last_seen_id, limit=50)
        events = events_resp.get("events") or []
        if isinstance(events, list) and events:
            new_last_id = last_seen_id or 0
            new_events_to_insert = []
            visits_to_insert = []

            for evt in events:
                evt_id = evt.get("id") or 0
                if evt_id > new_last_id:
                    new_last_id = evt_id

                evt_type = (evt.get("type") or "system").lower()
                msg = evt.get("message") or ""
                player = evt.get("player")
                origin = evt.get("island")
                evt_dodo = evt.get("dodoCode")
                nid = str(evt.get("nid") or evt.get("networkId") or "")
                evt_time = evt.get("timestamp") or datetime.now(timezone.utc).isoformat()

                new_events_to_insert.append((
                    evt_id,
                    clean_name,
                    evt_type,
                    msg,
                    player,
                    origin,
                    evt_dodo,
                    nid,
                    evt_time,
                ))

                if evt_type == "arrival" and player:
                    visits_to_insert.append((
                        player,
                        origin or "",
                        clean_name,
                        None,  # user_id resolved later
                        None,  # guild_id
                        1 if not is_sub else 1,
                        int(time.time()),
                        "sub" if is_sub else "free",
                        1,
                    ))

            with _CACHE_LOCK:
                _LAST_EVENT_IDS[clean_name.lower()] = new_last_id

            try:
                with connect_db() as conn:
                    if new_events_to_insert:
                        conn.execute("""
                            INSERT INTO island_events_log
                            (event_id, island_name, event_type, message, player, origin_island, dodo_code, network_id, timestamp)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, new_events_to_insert[0])
                        for row in new_events_to_insert[1:]:
                            conn.execute("""
                                INSERT INTO island_events_log
                                (event_id, island_name, event_type, message, player, origin_island, dodo_code, network_id, timestamp)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """, row)

                    if visits_to_insert:
                        for v in visits_to_insert:
                            conn.execute("""
                                INSERT INTO island_visits
                                (ign, origin_island, destination, user_id, guild_id, authorized, timestamp, island_type, has_island_access)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """, v)
            except Exception as exc:
                logger.debug(f"[SysBotTelemetry] Ingest error for {clean_name}: {exc}")

    return cached_entry


def poll_all_islands(max_workers: int = 12) -> Dict[str, Dict[str, Any]]:
    """Poll all 48 configured islands concurrently."""
    ensure_telemetry_schema()
    islands = getattr(Config, "ALL_ISLAND_PORT_ORDER", [])
    if not islands:
        return {}

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        list(executor.map(poll_single_island, islands))

    # Update island_bot_status in DB so legacy readers see presence without Discord
    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        with connect_db() as conn:
            with _CACHE_LOCK:
                for isl_lower, data in _TELEMETRY_CACHE.items():
                    conn.execute("""
                        INSERT INTO island_bot_status (island_id, is_online, updated_at)
                        VALUES (?, ?, ?)
                        ON CONFLICT(island_id) DO UPDATE SET
                            is_online = excluded.is_online,
                            updated_at = excluded.updated_at
                    """, (isl_lower, 1 if data.get("is_online") else 0, now_iso))
    except Exception as exc:
        logger.debug(f"[SysBotTelemetry] Failed to sync island_bot_status: {exc}")

    return get_all_cached_telemetry(mask_sub_dodo=True)


def _telemetry_worker_loop():
    """Background worker daemon thread loop."""
    logger.info("[SysBotTelemetry] Starting background telemetry worker across 48 ports...")
    while not _WORKER_STOP_EVENT.is_set():
        try:
            poll_all_islands(max_workers=10)
        except Exception as exc:
            logger.warning(f"[SysBotTelemetry] Polling cycle exception: {exc}")
        _WORKER_STOP_EVENT.wait(timeout=3.5)


def start_telemetry_worker() -> None:
    """Start background worker thread if not already running."""
    global _WORKER_THREAD
    if _WORKER_THREAD and _WORKER_THREAD.is_alive():
        return

    ensure_telemetry_schema()
    _WORKER_STOP_EVENT.clear()
    _WORKER_THREAD = threading.Thread(target=_telemetry_worker_loop, name="SysBotTelemetryWorker", daemon=True)
    _WORKER_THREAD.start()


def stop_telemetry_worker() -> None:
    """Stop the background telemetry worker."""
    _WORKER_STOP_EVENT.set()


def get_cached_island_telemetry(island_name: str, mask_sub_dodo: bool = True) -> Optional[Dict[str, Any]]:
    """
    Retrieve cached island telemetry.
    If mask_sub_dodo is True and the island is a Sub Island, dodo_code is strictly returned as None.
    """
    clean_lower = (island_name or "").strip().lower()
    with _CACHE_LOCK:
        entry = _TELEMETRY_CACHE.get(clean_lower)
        if not entry:
            return None
        res = dict(entry)

    is_sub = _is_sub_island(clean_lower)
    if is_sub and mask_sub_dodo:
        res["dodo_code"] = None
    else:
        res["dodo_code"] = res.get("raw_dodo")

    return res


def get_all_cached_telemetry(mask_sub_dodo: bool = True) -> Dict[str, Dict[str, Any]]:
    """Retrieve all cached island telemetries."""
    with _CACHE_LOCK:
        items = {k: dict(v) for k, v in _TELEMETRY_CACHE.items()}

    result = {}
    for k, v in items.items():
        is_sub = _is_sub_island(k)
        if is_sub and mask_sub_dodo:
            v["dodo_code"] = None
        else:
            v["dodo_code"] = v.get("raw_dodo")
        result[k] = v
    return result


def get_flight_history(
    island_name: Optional[str] = None,
    ign: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """Retrieve historical visitor arrivals from SQLite island_visits."""
    ensure_telemetry_schema()
    query = "SELECT id, ign, origin_island, destination, authorized, timestamp, island_type FROM island_visits WHERE 1=1"
    params: List[Any] = []

    if island_name:
        query += " AND UPPER(destination) = ?"
        params.append(island_name.strip().upper())
    if ign:
        query += " AND ign LIKE ?"
        params.append(f"%{ign.strip()}%")

    query += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
    params.extend([min(limit, 200), max(0, offset)])

    with connect_db() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
        return [
            {
                "id": r["id"],
                "ign": r["ign"],
                "origin_island": r["origin_island"],
                "destination": r["destination"],
                "authorized": bool(r["authorized"]),
                "timestamp": r["timestamp"],
                "island_type": r["island_type"],
            }
            for r in rows
        ]


def get_recent_island_events(
    island_name: Optional[str] = None,
    event_type: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Retrieve recent activity events from island_events_log."""
    ensure_telemetry_schema()
    query = "SELECT id, event_id, island_name, event_type, message, player, origin_island, timestamp FROM island_events_log WHERE 1=1"
    params: List[Any] = []

    if island_name:
        query += " AND UPPER(island_name) = ?"
        params.append(island_name.strip().upper())
    if event_type:
        query += " AND event_type = ?"
        params.append(event_type.strip().lower())

    query += " ORDER BY timestamp DESC LIMIT ?"
    params.append(min(limit, 200))

    with connect_db() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
        return [
            {
                "id": r["id"],
                "event_id": r["event_id"],
                "island_name": r["island_name"],
                "event_type": r["event_type"],
                "message": r["message"],
                "player": r["player"],
                "origin_island": r["origin_island"],
                "timestamp": r["timestamp"],
            }
            for r in rows
        ]


def get_live_visitor_overview() -> Dict[str, Any]:
    """Provide a real-time summary of all active visitors across all 48 islands."""
    all_telemetry = get_all_cached_telemetry(mask_sub_dodo=True)
    total_visitors = 0
    active_islands = []

    for name, data in all_telemetry.items():
        v_list = data.get("visitor_list") or []
        v_count = data.get("visitors_count") if data.get("visitors_count") is not None else len(v_list)
        total_visitors += v_count
        if data.get("is_online"):
            active_islands.append({
                "island": data.get("island"),
                "is_online": True,
                "visitors_count": v_count,
                "visitors": v_list,
                "last_arrival": data.get("last_arrival"),
                "last_departure": data.get("last_departure"),
            })

    return {
        "success": True,
        "total_visitors": total_visitors,
        "active_islands_count": len(active_islands),
        "islands": active_islands,
    }
