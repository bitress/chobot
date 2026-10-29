"""Synchronous database helpers (call through asyncio.to_thread): panels, audit logs, last drop, wizard state, carts."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any
from utils.database import connect_db

from .constants import CART_TTL_SECONDS
from .state import _PANEL_CHANNELS_CACHE

logger = logging.getLogger("DropBotCog")


# ─── Database helpers (all synchronous; call through asyncio.to_thread) ────────

def _init_tables() -> None:
    """Ensure the panel table and the audit-log tables exist."""
    try:
        with connect_db() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS island_dropbot_panels (
                    channel_id  VARCHAR(64) PRIMARY KEY,
                    island_name VARCHAR(128) NOT NULL,
                    message_id  VARCHAR(64),
                    updated_at  VARCHAR(64)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS villager_injections_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    island_name TEXT,
                    villager_name TEXT,
                    plot_number INTEGER,
                    requested_by_user_id TEXT,
                    status TEXT,
                    response_message TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS island_drops_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    island_name TEXT,
                    items TEXT,
                    requested_by_user_id TEXT,
                    status TEXT,
                    response_message TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dodo_reveal_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT,
                    island_clean TEXT,
                    channel_id TEXT,
                    message_url TEXT,
                    username TEXT,
                    nickname TEXT,
                    created_at INTEGER
                )
                """
            )
            # Active drop cart per (user, island), so a restart or an expired menu doesn't lose it
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dropbot_carts (
                    user_id     TEXT NOT NULL,
                    island_name TEXT NOT NULL,
                    payload     TEXT NOT NULL,
                    updated_at  INTEGER NOT NULL,
                    PRIMARY KEY (user_id, island_name)
                )
                """
            )
            # Sanrio wizard resume state
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sanrio_wizard_state (
                    user_id TEXT NOT NULL,
                    island_name TEXT NOT NULL,
                    target_villager TEXT NOT NULL,
                    placeholder_villager TEXT NOT NULL,
                    step1_done INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER,
                    PRIMARY KEY (user_id, island_name)
                )
                """
            )
            with contextlib.suppress(Exception):
                conn.execute("CREATE INDEX IF NOT EXISTS idx_island_drops_log_island_id ON island_drops_log (island_name, id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_villager_inj_island_id ON villager_injections_log (island_name, id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_dodo_reveal_island_id ON dodo_reveal_messages (island_clean, id)")
            # Prune old logs (>30 days) and stale wizard states (>24h)
            with contextlib.suppress(Exception):
                conn.execute("DELETE FROM island_drops_log WHERE created_at < datetime('now', '-30 days')")
                conn.execute("DELETE FROM villager_injections_log WHERE created_at < datetime('now', '-30 days')")
                conn.execute("DELETE FROM sanrio_wizard_state WHERE updated_at < (strftime('%s', 'now') - 86400)")
                conn.execute("DELETE FROM dropbot_carts WHERE updated_at < (strftime('%s', 'now') - 86400)")
            conn.commit()
    except Exception as exc:
        logger.error("[DropBot] Failed to init tables: %s", exc)


def _upsert_panel(channel_id: int, island_name: str, message_id: int) -> None:
    try:
        with connect_db() as conn:
            conn.execute(
                """
                INSERT INTO island_dropbot_panels (channel_id, island_name, message_id, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(channel_id) DO UPDATE SET
                    island_name=excluded.island_name,
                    message_id=excluded.message_id,
                    updated_at=excluded.updated_at
                """,
                (str(channel_id), island_name, str(message_id), datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
    except Exception as exc:
        logger.error("[DropBot] Failed to upsert panel: %s", exc)


def _get_panel_row(channel_id: int) -> dict | None:
    try:
        with connect_db() as conn:
            row = conn.execute(
                "SELECT island_name, message_id FROM island_dropbot_panels WHERE channel_id = ?",
                (str(channel_id),),
            ).fetchone()
            return dict(row) if row else None
    except Exception:
        return None


def _load_all_panels() -> list[dict]:
    try:
        with connect_db() as conn:
            rows = conn.execute(
                "SELECT channel_id, island_name, message_id FROM island_dropbot_panels"
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []


def _panel_channel_ids(island: str) -> list[int]:
    """Channel ids that host a panel for `island`."""
    try:
        with connect_db() as conn:
            rows = conn.execute(
                "SELECT channel_id FROM island_dropbot_panels WHERE island_name = ?", (island,)
            ).fetchall()
        return [int(r["channel_id"]) for r in rows]
    except Exception:
        return []


def _panel_channel_ids_cached(island: str) -> list[int]:
    now = time.monotonic()
    hit = _PANEL_CHANNELS_CACHE.get(island)
    if hit and now - hit[0] < 60:
        return hit[1]
    res = _panel_channel_ids(island)
    _PANEL_CHANNELS_CACHE[island] = (now, res)
    return res


def _log_drop(island: str, user_id: int, items: str, ok: bool, message: str) -> None:
    try:
        with connect_db() as conn:
            conn.execute(
                """
                INSERT INTO island_drops_log (island_name, items, requested_by_user_id, status, response_message)
                VALUES (?, ?, ?, ?, ?)
                """,
                (island, items[:500], str(user_id), "queued" if ok else "failed", message[:500]),
            )
            conn.commit()
    except Exception as exc:
        logger.warning("[DropBot] Failed to write drop audit log: %s", exc)


# Last successful drop per user, for "Re-drop Last Cart" / "Load Last Cart".
#   payload: the exact string sent to SysBot (re-sent verbatim on re-drop)
#   entries: [(item name or hex, qty), ...] when known (needed to rebuild a cart), else None
_USER_LAST_DROPS: dict[int, dict[str, Any]] = {}


_LAST_DROP_MISSES: dict[int, float] = {}


LAST_DROP_CACHE_MAX = 2000


LAST_DROP_MISS_TTL = 300.0


_AUDIT_ITEMS_MAX = 500   # island_drops_log.items is truncated to this many characters


def _record_user_drop(
    user_id: int,
    island: str,
    payload: str,
    display_name: str,
    entries: list[tuple[str, int]] | None = None,
) -> None:
    """Remember a user's last successful drop (in memory, bounded)."""
    _USER_LAST_DROPS.pop(user_id, None)   # re-insert so dict order tracks recency
    _USER_LAST_DROPS[user_id] = {
        "island": island,
        "payload": payload,
        "display_name": display_name or "Previous drop",
        "entries": list(entries) if entries else None,
        "time": time.time(),
    }
    _LAST_DROP_MISSES.pop(user_id, None)
    while len(_USER_LAST_DROPS) > LAST_DROP_CACHE_MAX:
        _USER_LAST_DROPS.pop(next(iter(_USER_LAST_DROPS)))


def _peek_user_last_drop(user_id: int) -> dict[str, Any] | None:
    """Memory-only lookup; safe to call from view constructors and _rebuild()."""
    return _USER_LAST_DROPS.get(user_id)


def _get_user_last_drop(user_id: int) -> dict[str, Any] | None:
    """Retrieve user's last drop from memory, or fallback to database audit log."""
    hit = _USER_LAST_DROPS.get(user_id)
    if hit:
        return hit
    try:
        with connect_db() as conn:
            cursor = conn.execute(
                """
                SELECT island_name, items, response_message, created_at FROM island_drops_log
                WHERE requested_by_user_id = ?
                ORDER BY id DESC LIMIT 1
                """,
                (str(user_id),),
            )
            row = cursor.fetchone()
            if row:
                island = row.get("island_name") or ""
                payload = (row.get("items") or "").strip()
                display = row.get("response_message") or (f"Previous drop on {island}" if island else "Previous drop")
                drop_info = {
                    "island": island,
                    "payload": payload,
                    "display_name": display,
                    "entries": None,
                    "time": time.time(),
                }
                _USER_LAST_DROPS[user_id] = drop_info
                return drop_info
    except Exception as exc:
        logger.debug("[DropBot] Could not fetch last drop from DB for user %s: %s", user_id, exc)
    return None


def _fetch_last_drop_from_db(user_id: int) -> dict[str, Any] | None:
    """Blocking: newest successful drop in the audit log. Only the raw payload is recoverable."""
    with connect_db() as conn:
        row = conn.execute(
            """
            SELECT island_name, items FROM island_drops_log
            WHERE requested_by_user_id = ? AND status = 'queued'
            ORDER BY id DESC LIMIT 1
            """,
            (str(user_id),),
        ).fetchone()
    if not row:
        return None
    payload = (row.get("items") or "").strip()
    # The audit column is truncated; a payload at the limit may be cut mid-item, so never resend it.
    if not payload or len(payload) >= _AUDIT_ITEMS_MAX:
        return None
    island = row.get("island_name") or ""
    return {
        "island": island,
        "payload": payload,
        "display_name": f"Previous drop on {island}" if island else "Previous drop",
        "entries": None,
        "time": time.time(),
    }


async def _load_user_last_drop(user_id: int) -> dict[str, Any] | None:
    """Memory first, then the audit log (off the event loop). Misses are cached for a few minutes."""
    hit = _USER_LAST_DROPS.get(user_id)
    if hit:
        return hit
    miss_at = _LAST_DROP_MISSES.get(user_id)
    if miss_at and time.monotonic() - miss_at < LAST_DROP_MISS_TTL:
        return None
    try:
        info = await asyncio.to_thread(_fetch_last_drop_from_db, user_id)
    except Exception as exc:
        logger.debug("[DropBot] Could not fetch last drop for user %s: %s", user_id, exc)
        info = None
    if info:
        _record_user_drop(user_id, info["island"], info["payload"], info["display_name"], None)
        return _USER_LAST_DROPS[user_id]
    if len(_LAST_DROP_MISSES) > LAST_DROP_CACHE_MAX:
        _LAST_DROP_MISSES.clear()
    _LAST_DROP_MISSES[user_id] = time.monotonic()
    return None


def _log_injection(island: str, villager: str, plot: int | None, user_id: int, ok: bool, message: str) -> None:
    try:
        with connect_db() as conn:
            conn.execute(
                """
                INSERT INTO villager_injections_log
                (island_name, villager_name, plot_number, requested_by_user_id, status, response_message)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (island, villager, plot if plot is not None else 0, str(user_id),
                 "queued" if ok else "failed", message[:500]),
            )
            conn.commit()
    except Exception as exc:
        logger.warning("[DropBot] Failed to write injection audit log: %s", exc)


def _save_sanrio_wizard_state(
    user_id: int,
    island: str,
    target: str,
    placeholder: str,
    step1_done: bool,
) -> None:
    try:
        with connect_db() as conn:
            conn.execute(
                """
                INSERT INTO sanrio_wizard_state
                    (user_id, island_name, target_villager, placeholder_villager, step1_done, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, island_name) DO UPDATE SET
                    target_villager=excluded.target_villager,
                    placeholder_villager=excluded.placeholder_villager,
                    step1_done=excluded.step1_done,
                    updated_at=excluded.updated_at
                """,
                (str(user_id), island, target, placeholder, 1 if step1_done else 0, int(time.time())),
            )
            conn.commit()
    except Exception as exc:
        logger.warning("[DropBot] Could not save sanrio wizard state: %s", exc)


def _load_sanrio_wizard_state(user_id: int, island: str | None = None) -> dict[str, Any] | None:
    try:
        with connect_db() as conn:
            if island:
                row = conn.execute(
                    """
                    SELECT island_name, target_villager, placeholder_villager, step1_done, updated_at
                    FROM sanrio_wizard_state
                    WHERE user_id = ? AND island_name = ?
                    """,
                    (str(user_id), island),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT island_name, target_villager, placeholder_villager, step1_done, updated_at
                    FROM sanrio_wizard_state
                    WHERE user_id = ?
                    ORDER BY updated_at DESC LIMIT 1
                    """,
                    (str(user_id),),
                ).fetchone()

            if row:
                updated_at = row["updated_at"]
                ts = 0.0
                if isinstance(updated_at, (int, float)):
                    ts = float(updated_at)
                elif isinstance(updated_at, str):
                    try:
                        ts = datetime.fromisoformat(updated_at).timestamp()
                    except Exception:
                        ts = float(updated_at) if updated_at.isdigit() else 0.0
                if ts and (time.time() - ts > 7200):
                    _clear_sanrio_wizard_state(user_id, row["island_name"])
                    return None
                return {
                    "island": row["island_name"],
                    "island_name": row["island_name"],
                    "target_villager": row["target_villager"],
                    "placeholder_villager": row["placeholder_villager"],
                    "step1_done": bool(row["step1_done"]),
                    "updated_at": ts or time.time(),
                }
    except Exception as exc:
        logger.warning("[DropBot] Could not load sanrio wizard state: %s", exc)
    return None


def _clear_sanrio_wizard_state(user_id: int, island: str | None = None) -> None:
    try:
        with connect_db() as conn:
            if island:
                conn.execute(
                    "DELETE FROM sanrio_wizard_state WHERE user_id = ? AND island_name = ?",
                    (str(user_id), island),
                )
            else:
                conn.execute(
                    "DELETE FROM sanrio_wizard_state WHERE user_id = ?",
                    (str(user_id),),
                )
            conn.commit()
    except Exception as exc:
        logger.warning("[DropBot] Could not clear sanrio wizard state: %s", exc)


def _save_cart_rows(user_id: int, island: str, rows: list[dict[str, Any]]) -> None:
    """Blocking. An empty cart deletes the row."""
    try:
        with connect_db() as conn:
            if rows:
                conn.execute(
                    """
                    INSERT INTO dropbot_carts (user_id, island_name, payload, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(user_id, island_name) DO UPDATE SET
                        payload=excluded.payload, updated_at=excluded.updated_at
                    """,
                    (str(user_id), island, json.dumps(rows), int(time.time())),
                )
            else:
                conn.execute(
                    "DELETE FROM dropbot_carts WHERE user_id = ? AND island_name = ?", (str(user_id), island)
                )
            conn.commit()
    except Exception as exc:
        logger.warning("[DropBot] Could not save cart: %s", exc)


def _load_cart_rows(user_id: int, island: str) -> list[dict[str, Any]]:
    """Blocking. Returns [] when there is no cart or it is older than CART_TTL_SECONDS."""
    try:
        with connect_db() as conn:
            row = conn.execute(
                "SELECT payload, updated_at FROM dropbot_carts WHERE user_id = ? AND island_name = ?",
                (str(user_id), island),
            ).fetchone()
        if not row or time.time() - int(row["updated_at"] or 0) > CART_TTL_SECONDS:
            return []
        data = json.loads(row["payload"])
        return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []
    except Exception as exc:
        logger.warning("[DropBot] Could not load cart: %s", exc)
        return []
