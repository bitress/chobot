"""Persist parsed Dodo-code updates into the local ChoBot database."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import time

from utils.database import connect_db
from utils.helpers import clean_text


def persist_dodo_update(
    island_name: str,
    dodo_code: str = "",
    status: str = "ONLINE",
    channel_id: str | int | None = None,
    message_id: str | int | None = None,
    message_url: str | None = None,
    source: str = "orderbot",
    raw_excerpt: str = "",
) -> bool:
    """Save the latest Dodo code for an island.

    Existing island rows are updated by id/name.  If the streamer has not added
    the island yet, a minimal local row is created so it appears in the
    dashboard immediately.
    """
    island_name = (island_name or "").strip().upper()
    dodo_code = (dodo_code or "").strip().upper()
    status = (status or "UNKNOWN").strip().upper()
    if not island_name or (not dodo_code and status == "UNKNOWN"):
        return False

    island_id = clean_text(island_name) or island_name.lower().replace(" ", "-")
    now = datetime.now(timezone.utc).isoformat()
    created_at = int(time.time())
    channel_id_str = str(channel_id) if channel_id else None
    message_id_str = str(message_id) if message_id else None
    stored_code = dodo_code or None

    with connect_db() as db:
        row = db.execute(
            "SELECT id FROM islands WHERE LOWER(id) = LOWER(?) OR UPPER(name) = UPPER(?) LIMIT 1",
            (island_id, island_name),
        ).fetchone()
        if row:
            db.execute(
                """
                UPDATE islands
                SET dodo_code = ?, status = ?, updated_at = ?,
                    channel_id = COALESCE(channel_id, ?)
                WHERE id = ?
                """,
                (stored_code, _island_status(status), now, channel_id_str, row["id"]),
            )
        else:
            db.execute(
                """
                INSERT INTO islands
                    (id, name, type, items, theme, cat, description, seasonal,
                     status, visitors, dodo_code, map_url, updated_at, required_roles, channel_id)
                VALUES (?, ?, '', '[]', 'teal', 'public', '', '',
                        ?, 0, ?, NULL, ?, '[]', ?)
                """,
                (island_id, island_name, _island_status(status), stored_code, now, channel_id_str),
            )
        db.execute(
            """
            INSERT INTO dodo_captures
                (island_name, dodo_code, status, channel_id, message_id, message_url,
                 source, raw_excerpt, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                island_name,
                stored_code,
                status,
                channel_id_str,
                message_id_str,
                message_url or "",
                source or "orderbot",
                (raw_excerpt or "")[:1000],
                created_at,
            ),
        )
    return True


def recent_dodo_captures(limit: int = 10) -> list[dict]:
    with connect_db() as db:
        rows = db.execute(
            """
            SELECT island_name, dodo_code, status, channel_id, message_id,
                   message_url, source, raw_excerpt, created_at
            FROM dodo_captures
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (max(1, min(int(limit), 50)),),
        ).fetchall()
    return [dict(row.items()) for row in rows]


def mark_stale_dodo_codes(stale_minutes: int, new_status: str = "OFFLINE") -> int:
    """Clear old Dodo codes and mark islands stale/offline.

    Returns the number of island rows updated.
    """
    try:
        stale_minutes = int(stale_minutes)
    except (TypeError, ValueError):
        return 0
    if stale_minutes <= 0:
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=stale_minutes)
    now = datetime.now(timezone.utc).isoformat()
    status = _island_status(new_status.strip().upper() if new_status else "OFFLINE")

    with connect_db() as db:
        rows = db.execute(
            "SELECT id, updated_at FROM islands WHERE dodo_code IS NOT NULL AND dodo_code != ''",
        ).fetchall()
        stale_ids = []
        for row in rows:
            updated_at = _parse_iso_datetime(row["updated_at"])
            if updated_at and updated_at < cutoff:
                stale_ids.append(row["id"])
        for island_id in stale_ids:
            db.execute(
                "UPDATE islands SET dodo_code = NULL, status = ?, updated_at = ? WHERE id = ?",
                (status, now, island_id),
            )
    return len(stale_ids)


def _island_status(status: str) -> str:
    if status == "ONLINE":
        return "ONLINE"
    if status in {"REFRESHING", "ORDER_STARTING"}:
        return "REFRESHING"
    if status == "OFFLINE":
        return "OFFLINE"
    return status or "OFFLINE"


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
