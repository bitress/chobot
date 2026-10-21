"""
Island Presence Verification Module.

Enforces that a user must physically be present on a destination island
before item drop commands or payloads are dispatched to SysBot.
Uses the SysBot HTTP API (GET /api/status -> visitor_list) and telemetry
to verify visitor presence in real-time.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from utils.database import connect_db
from utils.helpers import clean_text

logger = logging.getLogger("IslandPresence")


def _extract_name_tokens(raw_value: str | None) -> list[str]:
    """Extract individual name tokens from compound strings like 'Alice | Island' or 'Alice / Bob | Island'."""
    if not raw_value:
        return []
    s = str(raw_value).strip()
    if not s:
        return []

    tokens: list[str] = [s]

    # First split on pipe
    pipe_parts = [p.strip() for p in s.split("|") if p.strip()]
    if len(pipe_parts) > 1:
        tokens.extend(pipe_parts)
        # First chunk before pipe is usually the IGN portion
        tokens.append(pipe_parts[0])

    # For every accumulated token, also split on slash and dash
    expanded: list[str] = []
    for tok in tokens:
        expanded.append(tok)
        if "/" in tok:
            expanded.extend([sub.strip() for sub in tok.split("/") if sub.strip()])
        if " - " in tok:
            expanded.extend([sub.strip() for sub in tok.split(" - ") if sub.strip()])
        # Strip parentheses e.g. 'Alice (Wonderland)' -> 'Alice'
        m = re.match(r"^([^\(\)]+?)\s*\([^\(\)]+\)$", tok)
        if m:
            expanded.append(m.group(1).strip())

    # Further split on slash for any remaining parts
    final_tokens: list[str] = []
    for tok in expanded:
        final_tokens.append(tok)
        if "/" in tok:
            final_tokens.extend([sub.strip() for sub in tok.split("/") if sub.strip()])

    # Filter out empty or whitespace tokens, preserve order with deduplication
    seen = set()
    result = []
    for t in final_tokens:
        clean_t = t.strip()
        if clean_t and clean_t.lower() not in seen:
            seen.add(clean_t.lower())
            result.append(clean_t)

    return result


def get_island_current_visitors_sync(island_name: str) -> list[str]:
    """
    Fetch the list of player visitor names currently present on the given island
    via the SysBot HTTP API synchronously. Falls back to cached telemetry.
    """
    clean_island = str(island_name or "").strip().title()
    if not clean_island:
        return []

    # 1. Direct query to island SysBot instance via SysBotClient
    try:
        from utils.sysbot_api import get_island_sysbot_client
        from utils.sysbot_telemetry import parse_sysbot_visitors

        client = get_island_sysbot_client(clean_island, timeout=3)
        status_data = client.get_bot_status_sync()
        if isinstance(status_data, dict):
            # Only parse if bot responded with a valid status payload
            _, visitors = parse_sysbot_visitors(status_data)
            if visitors:
                return visitors
            # If the bot is explicitly online and returned 0 visitors, return empty list
            if status_data.get("is_running") and status_data.get("is_connected"):
                return []
    except Exception as exc:
        logger.debug("[IslandPresence] Direct SysBot sync query failed for %s: %s", clean_island, exc)

    # 2. Fallback: Query cached telemetry snapshot
    try:
        from utils.sysbot_telemetry import get_cached_island_telemetry

        telem = get_cached_island_telemetry(clean_island, mask_sub_dodo=False)
        if telem and isinstance(telem.get("visitor_list"), list):
            return [str(x).strip() for x in telem["visitor_list"] if str(x).strip()]
    except Exception as exc:
        logger.debug("[IslandPresence] Cached telemetry lookup failed for %s: %s", clean_island, exc)

    return []


async def get_island_current_visitors_async(island_name: str) -> list[str]:
    """
    Fetch the list of player visitor names currently present on the given island
    via the SysBot HTTP API asynchronously. Falls back to cached telemetry.
    """
    clean_island = str(island_name or "").strip().title()
    if not clean_island:
        return []

    # 1. Direct query to island SysBot instance
    try:
        from utils.sysbot_api import get_island_sysbot_client
        from utils.sysbot_telemetry import parse_sysbot_visitors

        client = get_island_sysbot_client(clean_island, timeout=3)
        status_data = await client.get_bot_status()
        if isinstance(status_data, dict):
            _, visitors = parse_sysbot_visitors(status_data)
            if visitors:
                return visitors
            if status_data.get("is_running") and status_data.get("is_connected"):
                return []
    except Exception as exc:
        logger.debug("[IslandPresence] Direct SysBot async query failed for %s: %s", clean_island, exc)

    # 2. Fallback: Query cached telemetry snapshot
    try:
        from utils.sysbot_telemetry import get_cached_island_telemetry

        telem = get_cached_island_telemetry(clean_island, mask_sub_dodo=False)
        if telem and isinstance(telem.get("visitor_list"), list):
            return [str(x).strip() for x in telem["visitor_list"] if str(x).strip()]
    except Exception as exc:
        logger.debug("[IslandPresence] Cached telemetry lookup failed for %s: %s", clean_island, exc)

    return []


def extract_candidate_igns(
    user: Any = None,
    request_data: Optional[Dict[str, Any]] = None,
) -> list[str]:
    """
    Gather all plausible in-game player names (IGNs) for the requester from:
      1. Explicit request payload (ign, in_game_name, character, username).
      2. Database: user_saved_characters for the user's primary and linked IDs.
      3. User profile data: nickname, display_name, global_name, username.
      4. Database: recent authorized visits in island_visits.
    """
    candidates: list[str] = []
    user_ids: list[str] = []

    # 1. Payload arguments
    if isinstance(request_data, dict):
        for key in ("ign", "in_game_name", "character", "character_name", "player_name"):
            val = request_data.get(key)
            if val and isinstance(val, str) and val.strip():
                candidates.extend(_extract_name_tokens(val))

        # Include username if provided and not generic
        raw_user = request_data.get("username")
        if raw_user and isinstance(raw_user, str) and raw_user.strip().lower() not in ("webuser", "anonymous", ""):
            candidates.extend(_extract_name_tokens(raw_user))

    # 2. Discord User / Member instance
    if user is not None and not isinstance(user, dict):
        user_id = str(getattr(user, "id", "") or "").strip()
        if user_id:
            user_ids.append(user_id)

        display_name = getattr(user, "display_name", None)
        nick = getattr(user, "nick", None)
        name = getattr(user, "name", None)
        global_name = getattr(user, "global_name", None)

        for n in (display_name, nick, global_name, name):
            if n:
                candidates.extend(_extract_name_tokens(n))

    # 3. Auth user dictionary (from web token / session)
    elif isinstance(user, dict):
        # Resolve all possible user IDs for database lookup
        try:
            from utils.unified_auth import resolve_all_user_ids
            resolved = resolve_all_user_ids(user)
            if resolved:
                user_ids.extend([str(x) for x in resolved if str(x).strip()])
        except Exception:
            pass

        for field in ("user_id", "discord_id", "id", "sub", "uid"):
            val = user.get(field)
            if val:
                s_val = str(val).strip()
                if s_val and s_val not in user_ids:
                    user_ids.append(s_val)

        for field in ("nickname", "display_name", "global_name", "username"):
            val = user.get(field)
            if val and isinstance(val, str) and val.strip().lower() not in ("webuser", "anonymous", ""):
                candidates.extend(_extract_name_tokens(val))

    # 4. Lookup saved characters in database
    if user_ids:
        try:
            with connect_db() as conn:
                placeholders = ",".join("?" for _ in user_ids)
                rows = conn.execute(
                    f"SELECT ign FROM user_saved_characters WHERE user_id IN ({placeholders})",
                    user_ids,
                ).fetchall()
                for r in rows:
                    saved_ign = r["ign"] if hasattr(r, "__getitem__") else getattr(r, "ign", None)
                    if saved_ign:
                        candidates.extend(_extract_name_tokens(saved_ign))
        except Exception as exc:
            logger.debug("[IslandPresence] DB user_saved_characters lookup note: %s", exc)

    # 5. Lookup recent arrivals in island_visits
    if user_ids:
        try:
            with connect_db() as conn:
                placeholders = ",".join("?" for _ in user_ids)
                rows = conn.execute(
                    f"SELECT ign FROM island_visits WHERE user_id IN ({placeholders}) ORDER BY timestamp DESC LIMIT 5",
                    user_ids,
                ).fetchall()
                for r in rows:
                    v_ign = r["ign"] if hasattr(r, "__getitem__") else getattr(r, "ign", None)
                    if v_ign:
                        candidates.extend(_extract_name_tokens(v_ign))
        except Exception as exc:
            logger.debug("[IslandPresence] DB island_visits lookup note: %s", exc)

    # Filter out empty or single-character noise, preserve order with deduplication
    cleaned_candidates: list[str] = []
    seen = set()
    for c in candidates:
        stripped = c.strip()
        c_clean = clean_text(stripped)
        if len(c_clean) >= 2 and c_clean not in seen:
            seen.add(c_clean)
            cleaned_candidates.append(stripped)

    return cleaned_candidates


def match_ign_to_visitors(
    candidate_igns: Sequence[str],
    visitors: Sequence[str],
) -> Optional[str]:
    """
    Compare candidate in-game names against current island visitors.
    Uses clean_text for Unicode, accent-insensitive, and CJK-safe matching.
    Returns the matching visitor name from the island if found, else None.
    """
    if not candidate_igns or not visitors:
        return None

    # Pre-clean all visitors
    clean_visitors: list[Tuple[str, str]] = []
    for v in visitors:
        s = str(v).strip()
        if not s:
            continue
        # Strip "#N: " prefix if still present
        m = re.match(r"^#\d+:\s*(.+)$", s)
        name_str = m.group(1).strip() if m else s
        c = clean_text(name_str)
        if c:
            clean_visitors.append((s, c))

    if not clean_visitors:
        return None

    for candidate in candidate_igns:
        cand_clean = clean_text(candidate)
        if not cand_clean or len(cand_clean) < 2:
            continue

        for orig_v, v_clean in clean_visitors:
            # 1. Exact match
            if cand_clean == v_clean:
                return orig_v

            # 2. Word boundary or separator match
            if v_clean.startswith(cand_clean) or cand_clean.startswith(v_clean):
                return orig_v

            # 3. Substring match (e.g. "Alice" in "Alice from Wonderland" or "Alice (Wonderland)")
            if len(cand_clean) >= 3 and (cand_clean in v_clean or v_clean in cand_clean):
                return orig_v

    return None


def verify_user_on_island(
    island_name: str,
    user: Any = None,
    request_data: Optional[Dict[str, Any]] = None,
    allow_admin_bypass: bool = True,
) -> Tuple[bool, Optional[str], List[str]]:
    """
    Verify synchronously that the requester is present on the destination island.
    Returns: (is_present, matched_player_name, current_island_visitors)
    """
    data = request_data if isinstance(request_data, dict) else {}

    # Check for explicit Admin / Mod bypass flag
    is_admin_or_mod = False
    if isinstance(user, dict):
        is_admin_or_mod = bool(user.get("is_admin") or user.get("is_mod"))
    elif user is not None:
        is_admin_or_mod = bool(
            getattr(user, "guild_permissions", None) and getattr(user.guild_permissions, "administrator", False)
        )

    if allow_admin_bypass and is_admin_or_mod:
        if bool(data.get("admin_bypass") or data.get("bypass_presence")):
            visitors = get_island_current_visitors_sync(island_name)
            return True, "Admin Bypass", visitors

    visitors = get_island_current_visitors_sync(island_name)
    if not visitors:
        return False, None, []

    candidates = extract_candidate_igns(user=user, request_data=data)
    matched = match_ign_to_visitors(candidates, visitors)
    if matched:
        return True, matched, visitors

    return False, None, visitors


async def verify_user_on_island_async(
    island_name: str,
    user: Any = None,
    request_data: Optional[Dict[str, Any]] = None,
    allow_admin_bypass: bool = True,
) -> Tuple[bool, Optional[str], List[str]]:
    """
    Verify asynchronously that the requester is present on the destination island.
    Returns: (is_present, matched_player_name, current_island_visitors)
    """
    data = request_data if isinstance(request_data, dict) else {}

    is_admin_or_mod = False
    if isinstance(user, dict):
        is_admin_or_mod = bool(user.get("is_admin") or user.get("is_mod"))
    elif user is not None:
        is_admin_or_mod = bool(
            getattr(user, "guild_permissions", None) and getattr(user.guild_permissions, "administrator", False)
        )

    if allow_admin_bypass and is_admin_or_mod:
        if bool(data.get("admin_bypass") or data.get("bypass_presence")):
            visitors = await get_island_current_visitors_async(island_name)
            return True, "Admin Bypass", visitors

    visitors = await get_island_current_visitors_async(island_name)
    if not visitors:
        return False, None, []

    candidates = extract_candidate_igns(user=user, request_data=data)
    matched = match_ign_to_visitors(candidates, visitors)
    if matched:
        return True, matched, visitors

    return False, None, visitors
