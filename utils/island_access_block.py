"""Who may (re)gain the Island Access role right now.

Accepting the sub rules (website or Discord) grants the role back. A warn only removes access
until the rules are re-accepted, but a ban or timeout must keep it off until it ends, whether a
mod issued it through chobot (user_moderation_status) or with Discord's own Timeout button
(the member's communication_disabled_until, which chobot never records).

Kept free of Flask so the Discord bot process can import it too.
"""

from __future__ import annotations

import logging
import time
from typing import Iterable

from utils.database import connect_db

logger = logging.getLogger("IslandAccessBlock")


def check_user_active_restriction(user_id: str | None, discord_id: str | None = None) -> dict | None:
    """Check if user has an active ban or unexpired timeout across all candidate IDs.
    Returns restriction dict or None.
    """
    from utils.unified_auth import resolve_all_user_ids

    cand_ids = []
    if user_id:
        cand_ids.extend(resolve_all_user_ids(user_id))
    if discord_id and str(discord_id) not in cand_ids:
        cand_ids.extend(resolve_all_user_ids(discord_id))
    cand_ids = list(dict.fromkeys([str(c).strip() for c in cand_ids if c]))

    if not cand_ids:
        return None

    now = int(time.time())
    try:
        conn = connect_db()
        try:
            placeholders = ",".join("?" for _ in cand_ids)
            try:
                cur = conn.execute(
                    f"""
                    SELECT id, user_id, discord_id, action_type, reason, created_at, expires_at, mod_name
                    FROM user_moderation_status
                    WHERE is_active = 1
                      AND (user_id IN ({placeholders}) OR (discord_id IS NOT NULL AND discord_id IN ({placeholders})))
                      AND (expires_at IS NULL OR expires_at > ?)
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    cand_ids + cand_ids + [now],
                )
                row = cur.fetchone()
                if row:
                    return {
                        "id": row[0],
                        "user_id": row[1],
                        "discord_id": row[2],
                        "action_type": row[3],
                        "reason": row[4],
                        "created_at": row[5],
                        "expires_at": row[6],
                        "mod_name": row[7] or "Moderator",
                    }
            except Exception:
                cur = conn.execute(
                    f"""
                    SELECT id, user_id, discord_id, action_type, reason, created_at, expires_at
                    FROM user_moderation_status
                    WHERE is_active = 1
                      AND (user_id IN ({placeholders}) OR (discord_id IS NOT NULL AND discord_id IN ({placeholders})))
                      AND (expires_at IS NULL OR expires_at > ?)
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    cand_ids + cand_ids + [now],
                )
                row = cur.fetchone()
                if row:
                    return {
                        "id": row[0],
                        "user_id": row[1],
                        "discord_id": row[2],
                        "action_type": row[3],
                        "reason": row[4],
                        "created_at": row[5],
                        "expires_at": row[6],
                        "mod_name": "Moderator",
                    }
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("[IslandAccessBlock] Restriction check failed: %s", exc)
    return None


def discord_timeout_until(discord_id: str | int | None) -> int | None:
    """End of the member's Discord timeout (epoch seconds) if one is running, else None.

    Lookup failures return None: Discord being unreachable must not lock members out.
    """
    uid = str(discord_id or "").strip()
    if not uid.isdigit():
        return None
    from utils.discord_membership import fetch_guild_member_snapshot

    try:
        until = fetch_guild_member_snapshot(uid).get("timed_out_until")
    except Exception as exc:
        logger.debug("[IslandAccessBlock] Discord timeout lookup failed for %s: %s", uid, exc)
        return None
    return until if until and until > time.time() else None


def discord_timeout_restriction(until: int) -> dict:
    """A Discord-native timeout in the same shape check_user_active_restriction returns."""
    return {
        "id": None,
        "action_type": "timeout",
        "reason": "Timed out in Discord",
        "created_at": None,
        "expires_at": int(until),
        "mod_name": "Discord",
        "source": "discord",
    }


def island_access_block(user_id: str | None, discord_ids: Iterable[str | int | None] = ()) -> dict | None:
    """Why this member can't get Island Access back right now, or None if they can.

    Checks chobot bans/timeouts for every linked ID, then a running Discord timeout on each
    Discord account.
    """
    ids = [str(d).strip() for d in discord_ids if str(d or "").strip().isdigit()]
    restriction = check_user_active_restriction(user_id, ids[0] if ids else None)
    if not restriction:
        for extra in ids[1:]:
            restriction = check_user_active_restriction(None, extra)
            if restriction:
                break
    if restriction:
        return restriction
    for did in dict.fromkeys(ids):
        until = discord_timeout_until(did)
        if until:
            return discord_timeout_restriction(until)
    return None
