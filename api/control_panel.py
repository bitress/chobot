"""
Control Panel Module
Standalone moderation cockpit API for Sub-Island live flights, Dodo code reveals,
identity verification, and moderation actions.

DO NOT TOUCH api/dashboard.py - this module is completely decoupled and self-contained.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import threading
import time
import unicodedata
import urllib.parse
from functools import wraps
from typing import Any, Dict, List, Optional, Tuple

from flask import Blueprint, jsonify, request, session
from thefuzz import fuzz

from utils.auth_tokens import get_auth_user
from utils.config import Config
from utils.database import connect_db, get_backend
from utils.helpers import clean_text
from utils.website_identity import (
    SAVED_CHARACTERS_SQL,
    account_label,
    saved_character_owners,
    website_only_account,
    website_verdict,
)

logger = logging.getLogger("ControlPanel")

control_panel_bp = Blueprint("control_panel", __name__, url_prefix="/api/control-panel")

# -----------------------------------------------------------------------------
# Database Schema Initialization & Migrations
# -----------------------------------------------------------------------------

_schema_ready = False
_schema_lock = threading.Lock()


def init_control_panel_db(force: bool = False) -> None:
    """Safely apply migrations for Control Panel tables and columns across SQLite and MySQL.

    Runs at API startup; later calls are no-ops once it has succeeded (it used to run
    its column checks on every live-feed refresh).
    """
    global _schema_ready
    if _schema_ready and not force:
        return
    with _schema_lock:
        if _schema_ready and not force:
            return
        _apply_control_panel_migrations()


def _apply_control_panel_migrations() -> None:
    global _schema_ready
    try:
        conn = connect_db()
        try:
            backend = get_backend()
            existing_cols = set()
            if backend == "mysql":
                try:
                    cur = conn.execute("SHOW COLUMNS FROM island_visits")
                    existing_cols = {str(row[0]).lower() for row in cur.fetchall()}
                except Exception as exc:
                    logger.warning("[ControlPanel] Could not inspect columns via SHOW COLUMNS: %s", exc)
            else:
                try:
                    cur = conn.execute("PRAGMA table_info(island_visits)")
                    existing_cols = {str(row[1]).lower() for row in cur.fetchall()}
                except Exception as exc:
                    logger.warning("[ControlPanel] Could not inspect columns via PRAGMA: %s", exc)

            new_cols = [
                ("review_status", "VARCHAR(32) DEFAULT 'pending'"),
                ("handled_by", "VARCHAR(255) DEFAULT NULL"),
                ("handled_at", "BIGINT DEFAULT NULL"),
                ("match_score", "INTEGER DEFAULT NULL"),
                ("correlated_reveal_id", "BIGINT DEFAULT NULL"),
                # Written by bots/flight_logger.py: why it authorised or flagged the visit
                ("logger_verdict", "VARCHAR(48) DEFAULT NULL"),
                ("logger_detail", "TEXT DEFAULT NULL"),
                # The #flight-logs alert for the visit, edited when the web resolves it
                ("alert_channel_id", "VARCHAR(32) DEFAULT NULL"),
                ("alert_message_id", "VARCHAR(32) DEFAULT NULL"),
            ]
            for col_name, col_def in new_cols:
                if col_name.lower() not in existing_cols:
                    try:
                        conn.execute(f"ALTER TABLE island_visits ADD COLUMN {col_name} {col_def}")
                        conn.commit()
                    except Exception:
                        pass  # Column already exists or table not ready

            if backend == "mysql" and "review_status" in existing_cols:
                try:
                    conn.execute(
                        "UPDATE island_visits SET review_status = "
                        "CASE WHEN authorized = 1 THEN 'authorised' ELSE 'pending' END "
                        "WHERE review_status IS NULL OR review_status = ''"
                    )
                    conn.commit()
                except Exception as exc:
                    logger.warning(
                        "[ControlPanel] Could not backfill empty review_status values: %s",
                        exc,
                    )
                try:
                    conn.execute(
                        "ALTER TABLE island_visits MODIFY COLUMN review_status "
                        "VARCHAR(32) NOT NULL DEFAULT 'pending'"
                    )
                    conn.commit()
                except Exception as exc:
                    logger.warning(
                        "[ControlPanel] Could not normalize review_status default: %s",
                        exc,
                    )

            try:
                conn.execute("CREATE INDEX ix_island_visits_review_status ON island_visits(review_status)")
                conn.commit()
            except Exception:
                pass

            # Backfill existing visits if review_status is empty. Never touch 'pending':
            # flagged visits keep authorized = 1 for the bot's 24h follow-up logic.
            try:
                conn.execute(
                    "UPDATE island_visits SET review_status = 'authorised' "
                    "WHERE authorized = 1 AND review_status IS NULL"
                )
                conn.execute("UPDATE island_visits SET review_status = 'pending' WHERE review_status IS NULL")
                conn.commit()
            except Exception:
                pass

            # 2. Moderation enforcement status table
            conn.execute("""
            CREATE TABLE IF NOT EXISTS user_moderation_status (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id VARCHAR(64) NOT NULL,
                discord_id VARCHAR(64) DEFAULT NULL,
                action_type VARCHAR(64) NOT NULL,
                reason TEXT NOT NULL,
                mod_id VARCHAR(64) NOT NULL,
                created_at BIGINT NOT NULL,
                expires_at BIGINT DEFAULT NULL,
                is_active INTEGER DEFAULT 1,
                reverted_at BIGINT DEFAULT NULL,
                reverted_by VARCHAR(255) DEFAULT NULL
            )
            """)
            conn.commit()

            try:
                conn.execute("CREATE INDEX ix_mod_status_user ON user_moderation_status(user_id, is_active)")
                conn.commit()
            except Exception:
                pass

            try:
                conn.execute("CREATE INDEX ix_mod_status_discord ON user_moderation_status(discord_id, is_active)")
                conn.commit()
            except Exception:
                pass

            # 3. Warnings table extensions (acknowledged_at, mod_name)
            warn_cols = set()
            if backend == "mysql":
                try:
                    cur = conn.execute("SHOW COLUMNS FROM warnings")
                    warn_cols = {str(row[0]).lower() for row in cur.fetchall()}
                except Exception:
                    pass
            else:
                try:
                    cur = conn.execute("PRAGMA table_info(warnings)")
                    warn_cols = {str(row[1]).lower() for row in cur.fetchall()}
                except Exception:
                    pass

            for col_name, col_def in [
                ("acknowledged_at", "BIGINT DEFAULT NULL"),
                ("mod_name", "VARCHAR(255) DEFAULT NULL"),
                # Website accounts outside Discord (user_id is a numeric Discord ID)
                ("web_user_id", "VARCHAR(64) DEFAULT NULL"),
            ]:
                if col_name.lower() not in warn_cols:
                    try:
                        conn.execute(f"ALTER TABLE warnings ADD COLUMN {col_name} {col_def}")
                        conn.commit()
                    except Exception:
                        pass
            try:
                conn.execute("CREATE INDEX ix_warnings_web_user_id ON warnings(web_user_id)")
                conn.commit()
            except Exception:
                pass  # Index already exists

            # 4. user_moderation_status extensions (mod_name)
            mod_status_cols = set()
            if backend == "mysql":
                try:
                    cur = conn.execute("SHOW COLUMNS FROM user_moderation_status")
                    mod_status_cols = {str(row[0]).lower() for row in cur.fetchall()}
                except Exception:
                    pass
            else:
                try:
                    cur = conn.execute("PRAGMA table_info(user_moderation_status)")
                    mod_status_cols = {str(row[1]).lower() for row in cur.fetchall()}
                except Exception:
                    pass

            if "mod_name" not in mod_status_cols:
                try:
                    conn.execute("ALTER TABLE user_moderation_status ADD COLUMN mod_name VARCHAR(255) DEFAULT NULL")
                    conn.commit()
                except Exception:
                    pass

            # 5. Idempotency store
            conn.execute("""
            CREATE TABLE IF NOT EXISTS control_panel_idempotency (
                `key` VARCHAR(255) PRIMARY KEY,
                response_json TEXT NOT NULL,
                created_at BIGINT NOT NULL
            )
            """)
            conn.commit()

            # What reached Discord for each web action (shown on the flight card)
            conn.execute("""
            CREATE TABLE IF NOT EXISTS control_panel_discord_sync (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                visit_id BIGINT NOT NULL,
                action VARCHAR(32) NOT NULL,
                case_id VARCHAR(32) DEFAULT NULL,
                dm_status VARCHAR(16) DEFAULT NULL,
                case_log_url TEXT DEFAULT NULL,
                case_log_status VARCHAR(16) DEFAULT NULL,
                alert_status VARCHAR(16) DEFAULT NULL,
                created_at BIGINT NOT NULL
            )
            """)
            try:
                conn.execute("CREATE INDEX ix_cp_discord_sync_visit ON control_panel_discord_sync(visit_id)")
            except Exception:
                pass  # Index already exists
            conn.commit()

            logger.info("[ControlPanel] Database migrations verified.")
            _schema_ready = True
        finally:
            conn.close()
    except Exception as exc:
        logger.error("[ControlPanel] DB initialization failed: %s", exc, exc_info=True)


# -----------------------------------------------------------------------------
# Active Restriction Checker & Moderation Notices
# -----------------------------------------------------------------------------

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
        logger.warning("[ControlPanel] Restriction check failed: %s", exc)
    return None


def get_user_moderation_notices(user_or_id: Any) -> dict:
    """Return active restrictions and unacknowledged warnings for a user."""
    from utils.unified_auth import resolve_all_user_ids

    user_dict = user_or_id if isinstance(user_or_id, dict) else {}
    cand_ids = resolve_all_user_ids(user_or_id)
    cand_ids = list(dict.fromkeys([str(c).strip() for c in cand_ids if c]))
    if not cand_ids:
        return {
            "has_active_restriction": False,
            "active_restriction": None,
            "unacknowledged_warnings": [],
            "warning_count": 0,
        }

    user_id = user_dict.get("user_id") if user_dict else (str(user_or_id) if not isinstance(user_or_id, dict) else None)
    discord_id = user_dict.get("discord_id") if user_dict else None
    active_rest = check_user_active_restriction(user_id, discord_id)

    unack_warnings: list[dict] = []
    total_warnings = 0
    try:
        conn = connect_db()
        try:
            placeholders = ",".join("?" for _ in cand_ids)
            try:
                cur = conn.execute(
                    f"""
                    SELECT id, reason, mod_id, timestamp, action_type, acknowledged_at, mod_name, visit_id
                    FROM warnings
                    WHERE user_id IN ({placeholders}) OR web_user_id IN ({placeholders})
                    ORDER BY timestamp DESC
                    """,
                    cand_ids + cand_ids,
                )
                rows = cur.fetchall()
                for r in rows:
                    act = str(r[4] or "WARN").upper()
                    if act == "WARN":
                        total_warnings += 1
                        is_ack = bool(r[5])
                        if not is_ack:
                            unack_warnings.append({
                                "id": r[0],
                                "reason": r[1] or "Official warning from moderation",
                                "mod_id": r[2],
                                "timestamp": r[3],
                                "action_type": "WARN",
                                "acknowledged_at": r[5],
                                "mod_name": r[6] or "Moderator",
                                "visit_id": r[7],
                            })
            except Exception:
                cur = conn.execute(
                    f"""
                    SELECT id, reason, mod_id, timestamp, action_type, visit_id
                    FROM warnings
                    WHERE user_id IN ({placeholders})
                    ORDER BY timestamp DESC
                    """,
                    cand_ids,
                )
                rows = cur.fetchall()
                for r in rows:
                    act = str(r[4] or "WARN").upper()
                    if act == "WARN":
                        total_warnings += 1
                        unack_warnings.append({
                            "id": r[0],
                            "reason": r[1] or "Official warning from moderation",
                            "mod_id": r[2],
                            "timestamp": r[3],
                            "action_type": "WARN",
                            "acknowledged_at": None,
                            "mod_name": "Moderator",
                            "visit_id": r[5],
                        })
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("[ControlPanel] Error fetching user moderation notices: %s", exc)

    return {
        "has_active_restriction": bool(active_rest),
        "active_restriction": active_rest,
        "unacknowledged_warnings": unack_warnings,
        "warning_count": total_warnings,
    }


# -----------------------------------------------------------------------------
# Authentication & Permission Guards
# -----------------------------------------------------------------------------

def _get_current_user() -> dict | None:
    """Extract authenticated user payload from Bearer token or session."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[len("Bearer "):].strip()
        user = get_auth_user(token)
        if user:
            return user

    # Session fallback
    if session.get("user"):
        return session.get("user")
    if session.get("user_id"):
        return {
            "user_id": session.get("user_id"),
            "username": session.get("username", "Unknown"),
            "is_admin": session.get("is_admin", False),
            "is_mod": session.get("is_mod", False),
            "roles": session.get("roles", []),
        }
    return None


def _is_mod_user(user: dict | None) -> bool:
    if not user:
        return False
    if user.get("is_admin") or user.get("is_mod"):
        return True
    roles = {str(r) for r in user.get("roles", [])}
    mod_roles = {
        str(Config.ADMIN_ROLE_ID or ""),
        str(Config.SENIOR_MOD_ROLE_ID or ""),
        str(Config.BABY_MOD_ROLE_ID or ""),
    } - {"", "0", "None"}
    return bool(roles & mod_roles)


def _is_admin_user(user: dict | None) -> bool:
    if not user:
        return False
    if user.get("is_admin"):
        return True
    roles = {str(r) for r in user.get("roles", [])}
    admin_roles = {str(Config.ADMIN_ROLE_ID or "")} - {"", "0", "None"}
    return bool(roles & admin_roles)


def require_mod_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        # Allow internal token if matching DASHBOARD_SECRET
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and Config.DASHBOARD_SECRET:
            token = auth[len("Bearer "):].strip()
            if secrets.compare_digest(token, Config.DASHBOARD_SECRET):
                return f(*args, **kwargs)

        user = _get_current_user()
        if not user:
            return jsonify({"error": "Unauthorized", "code": "unauthorized"}), 401
        if not _is_mod_user(user):
            return jsonify({"error": "Forbidden: Moderator role required", "code": "forbidden"}), 403
        return f(*args, **kwargs)
    return decorated


def require_admin_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and Config.DASHBOARD_SECRET:
            token = auth[len("Bearer "):].strip()
            if secrets.compare_digest(token, Config.DASHBOARD_SECRET):
                return f(*args, **kwargs)

        user = _get_current_user()
        if not user:
            return jsonify({"error": "Unauthorized"}), 401
        if not _is_admin_user(user):
            return jsonify({"error": "Forbidden: Admin role required for this action"}), 403
        return f(*args, **kwargs)
    return decorated


# -----------------------------------------------------------------------------
# Correlator Engine: Fuzzy Matching, Tie-Breaking & Normalization
# -----------------------------------------------------------------------------

EMOJI_PATTERN = re.compile(
    r"[\U00010000-\U0010ffff\u2600-\u27BF\uE000-\uF8FF\uD83C-\uDBFF\uDC00-\uDFFF]"
)

def normalize_text_for_comparison(text: str | None) -> str:
    """Normalize Unicode (NFKD), strip emoji, punctuation and lowercase."""
    if not text:
        return ""
    # Strip emojis
    s = EMOJI_PATTERN.sub("", text)
    # NFKD decompose
    s = unicodedata.normalize("NFKD", s)
    # Strip combining marks
    s = "".join(c for c in s if not unicodedata.combining(c))
    # Strip non-alphanumeric except spaces
    s = re.sub(r"[^\w\s]", "", s, flags=re.UNICODE)
    return re.sub(r"\s+", " ", s).strip().lower()


def compare_identity(
    flight_ign: str | None,
    flight_origin: str | None,
    target_ign: str | None,
    target_island: str | None,
) -> tuple[int, bool, bool]:
    """Calculate 0-100 match score and boolean flags for IGN & Island.
    Returns: (score, ign_matched, isl_matched)
    """
    f_ign = normalize_text_for_comparison(flight_ign)
    f_isl = normalize_text_for_comparison(flight_origin)
    t_ign = normalize_text_for_comparison(target_ign)
    t_isl = normalize_text_for_comparison(target_island)

    if not f_ign or not t_ign:
        return 0, False, False

    ign_ratio = fuzz.ratio(f_ign, t_ign)
    isl_ratio = fuzz.ratio(f_isl, t_isl) if (f_isl and t_isl) else 50

    ign_matched = (f_ign == t_ign) or (ign_ratio >= 88)
    isl_matched = ((f_isl == t_isl) or (isl_ratio >= 80)) if (f_isl and t_isl) else False

    if ign_matched and isl_matched:
        exact_score = 100 if (f_ign == t_ign and f_isl == t_isl) else 95
        return exact_score, True, True

    # Weight character name 70%, origin island 30%
    weighted = int((ign_ratio * 0.70) + (isl_ratio * 0.30))

    if ign_matched and isl_ratio >= 70:
        return max(weighted, 90), True, False

    return min(100, max(0, weighted)), ign_matched, isl_matched


def compute_identity_match_score(
    flight_ign: str,
    flight_origin: str,
    target_ign: str,
    target_island: str,
) -> int:
    """Calculate 0-100 match score using Levenshtein fuzzy comparison."""
    score, _, _ = compare_identity(flight_ign, flight_origin, target_ign, target_island)
    return score


# Re-joins of the same IGN to the same island within this window are one alert
# (mirrors MAX_ALERT_MERGE_SECONDS in bots/flight_logger.py).
REJOIN_MERGE_SECONDS = 1800

# The radar only covers sub islands. /api/islands is the source of truth: an
# anonymous request reports every sub island with status "SUB ONLY".
SUB_ISLAND_CACHE_SECONDS = 300
_sub_island_cache: dict[str, Any] = {"names": None, "fetched_at": 0.0}


def _web_strikes(conn) -> dict[str, int]:
    """Strike counts for website accounts outside Discord, keyed like the Discord ones."""
    try:
        return {
            str(r[0]): r[1]
            for r in conn.execute(
                "SELECT web_user_id, COUNT(*) FROM warnings WHERE web_user_id IS NOT NULL "
                "AND action_type IN ('WARN', 'TIMEOUT', 'BAN') GROUP BY web_user_id"
            ).fetchall()
        }
    except Exception:
        return {}  # Column not migrated yet


# Verdicts written by bots/flight_logger.py -> how the dashboard presents them
LOGGER_VERDICT_KIND = {
    "verified": "verified",
    "ambiguous_resolved": "verified",
    "authorized_followup": "verified",
    "unknown_traveler": "unknown",
    "ambiguous_match": "ambiguous",
    "identity_limit": "flagged",
    "island_access_mismatch": "flagged",
    "recent_identity_change": "flagged",
    "flagged_by_mod": "flagged",
}
LOGGER_FLAG_VERDICTS = tuple(v for v, kind in LOGGER_VERDICT_KIND.items() if kind != "verified")

# Telemetry rows the flight logger never took over. New ones are 'unchecked'; older
# ones were written as 'authorised' with no member, guild, verdict or moderator.
def unchecked_sql(alias: str = "v.") -> str:
    """SQL condition for unchecked visits; pass alias="" for queries without a table alias."""
    a = alias
    return (
        f"({a}review_status = 'unchecked' OR ({a}logger_verdict IS NULL AND {a}user_id IS NULL "
        f"AND {a}guild_id IS NULL AND {a}handled_by IS NULL))"
    )


UNCHECKED_SQL = unchecked_sql()


def classify_logger_verdict(flight: dict) -> tuple[str, dict]:
    """Return (alert_kind, detail) for a visit from the flight logger's stored verdict."""
    try:
        detail = json.loads(flight.get("logger_detail") or "{}") or {}
    except (TypeError, ValueError):
        detail = {}
    verdict = flight.get("logger_verdict")
    if verdict:
        return LOGGER_VERDICT_KIND.get(verdict, "flagged"), detail
    if flight.get("review_status") == "unchecked" or not (
        flight.get("user_id") or flight.get("guild_id") or flight.get("handled_by")
    ):
        return "unchecked", detail
    # Logged by the flight logger before verdicts were stored
    return ("verified" if flight.get("authorized") else "unknown"), detail


def get_sub_island_names() -> set[str]:
    """Lowercased names of the islands /api/islands reports as "SUB ONLY"."""
    now = time.time()
    cached = _sub_island_cache["names"]
    if cached is not None and now - _sub_island_cache["fetched_at"] < SUB_ISLAND_CACHE_SECONDS:
        return cached

    try:
        import requests

        # No auth header on purpose: viewers without access see sub islands as "SUB ONLY".
        resp = requests.get(f"{Config.API_BASE_URL}/api/islands", timeout=8)
        resp.raise_for_status()
        payload = resp.json()
        items = payload.get("data", []) if isinstance(payload, dict) else payload
        names = {
            str(item.get("name") or "").strip().lower()
            for item in items
            if isinstance(item, dict) and str(item.get("status") or "").upper() == "SUB ONLY"
        }
        names.discard("")
        if names:
            _sub_island_cache.update(names=names, fetched_at=now)
            return names
        logger.warning("[ControlPanel] /api/islands reported no SUB ONLY islands; keeping previous list")
    except Exception as exc:
        logger.warning("[ControlPanel] Could not load sub islands from /api/islands: %s", exc)

    if cached is not None:
        return cached
    # Last resort until the API answers: the configured sub-island list
    return {clean_text(name).lower() for name in (getattr(Config, "SUB_ISLANDS", []) or [])}


def sub_destination_filter(column: str = "v.destination") -> tuple[str, list[str]]:
    """SQL clause + params restricting a query to flights whose destination is a sub island."""
    names = sorted(get_sub_island_names())
    if not names:
        return "1=0", []
    return f"LOWER({column}) IN ({','.join('?' for _ in names)})", names


def is_sub_island_destination(destination: str | None, island_type: str | None = None) -> bool:
    """Check if destination is a Sub-Island per /api/islands ("SUB ONLY")."""
    dest = (destination or "").strip().lower()
    if not dest:
        return False
    sub_names = get_sub_island_names()
    return dest in sub_names or clean_text(dest).lower() in sub_names


def get_user_characters_with_aliases(
    user_id: str | None,
    saved_chars_by_user: dict[str, list[dict]],
    user_alias_map: dict[str, set[str]],
) -> list[dict]:
    """Retrieve saved characters for a user ID, resolving any linked provider aliases."""
    if not user_id:
        return []
    uid_str = str(user_id).strip()
    if uid_str in saved_chars_by_user:
        return saved_chars_by_user[uid_str]
    for alias in user_alias_map.get(uid_str, set()):
        if alias in saved_chars_by_user:
            return saved_chars_by_user[alias]
    return []


def get_user_strike_count(
    user_id: str | None,
    strikes_map: dict[str, int],
    user_alias_map: dict[str, set[str]],
) -> int:
    """Get total active warnings/strikes for user across all aliases."""
    if not user_id:
        return 0
    uid_str = str(user_id).strip()
    if uid_str in strikes_map:
        return strikes_map[uid_str]
    for alias in user_alias_map.get(uid_str, set()):
        if alias in strikes_map:
            return strikes_map[alias]
    return 0


def get_user_active_restriction(
    user_id: str | None,
    active_restr_by_user: dict[str, dict],
    user_alias_map: dict[str, set[str]],
) -> dict | None:
    """Get active TIMEOUT/BAN restriction record for user across aliases."""
    if not user_id:
        return None
    uid_str = str(user_id).strip()
    if uid_str in active_restr_by_user:
        return active_restr_by_user[uid_str]
    for alias in user_alias_map.get(uid_str, set()):
        if alias in active_restr_by_user:
            return active_restr_by_user[alias]
    return None


def evaluate_flight_against_user_characters(
    flight_ign: str,
    flight_origin: str,
    user_chars: list[dict],
) -> dict:
    """
    Match an incoming flight against a user's saved in-game characters.
    The character MUST be the active one (is_default == True).
    If it matches an inactive slot, has island mismatch, IGN mismatch, or no saved character,
    flag it accordingly with appropriate match_status and capped match_score (< 90).
    """
    if not user_chars:
        return {
            "is_flagged": True,
            "flag_reason": "no_saved_character",
            "flag_message": "Flagged: Revealing user has no saved in-game character in profile",
            "match_status": "mismatch",
            "score": 0,
            "matched_character": None,
            "active_character": None,
        }

    # Find the active / default character slot
    active_char = next((c for c in user_chars if c.get("is_default")), None)

    if active_char:
        act_score, act_ign_match, act_isl_match = compare_identity(
            flight_ign, flight_origin, active_char["ign"], active_char["island_name"]
        )

        # Full match with active character
        if act_ign_match and act_isl_match:
            return {
                "is_flagged": False,
                "flag_reason": None,
                "flag_message": None,
                "match_status": "verified",
                "score": max(act_score, 90),
                "matched_character": active_char,
                "active_character": active_char,
            }

        # Did NOT fully match active character. Check if it matches an INACTIVE saved character slot!
        inactive_match = None
        for c in user_chars:
            if c.get("is_default"):
                continue
            c_score, c_ign_match, c_isl_match = compare_identity(
                flight_ign, flight_origin, c["ign"], c["island_name"]
            )
            if c_ign_match and c_isl_match:
                inactive_match = (c, max(c_score, 70), "full")
                break
            elif c_ign_match and (inactive_match is None or c_score > inactive_match[1]):
                inactive_match = (c, c_score, "ign_only")

        if inactive_match:
            # Matched inactive character slot! Must flag it!
            matched_c, raw_score, _ = inactive_match
            return {
                "is_flagged": True,
                "flag_reason": "inactive_character",
                "flag_message": f"Flagged: Inactive character slot '{matched_c['ign']}' used. Active character is '{active_char['ign']}'.",
                "match_status": "partial",
                "score": min(raw_score, 75),  # Capped strictly < 90 so bulk-authorise will NOT approve
                "matched_character": matched_c,
                "active_character": active_char,
            }

        # Check partial relation to active character:
        if act_ign_match:
            return {
                "is_flagged": True,
                "flag_reason": "island_mismatch",
                "flag_message": f"Flagged: Island mismatch (Flight: '{flight_origin}' vs Active: '{active_char['island_name']}').",
                "match_status": "partial",
                "score": min(act_score, 70),
                "matched_character": active_char,
                "active_character": active_char,
            }
        elif act_isl_match:
            return {
                "is_flagged": True,
                "flag_reason": "ign_mismatch",
                "flag_message": f"Flagged: IGN mismatch (Flight: '{flight_ign}' vs Active: '{active_char['ign']}').",
                "match_status": "partial",
                "score": min(act_score, 50),
                "matched_character": active_char,
                "active_character": active_char,
            }
        else:
            return {
                "is_flagged": True,
                "flag_reason": "character_mismatch",
                "flag_message": f"Flagged: Neither IGN nor Island match active character '{active_char['ign']}' from '{active_char['island_name']}'.",
                "match_status": "mismatch",
                "score": min(act_score, 25),
                "matched_character": active_char,
                "active_character": active_char,
            }

    # No character marked is_default (user has saved characters, but none set as active)
    best_c = None
    best_c_score = 0
    for c in user_chars:
        c_score, c_ign, c_isl = compare_identity(flight_ign, flight_origin, c["ign"], c["island_name"])
        if c_score > best_c_score:
            best_c_score = c_score
            best_c = c

    return {
        "is_flagged": True,
        "flag_reason": "no_active_character",
        "flag_message": "Flagged: User has saved characters, but none is set as active.",
        "match_status": "partial" if best_c_score >= 70 else "mismatch",
        "score": min(best_c_score, 60) if best_c_score >= 70 else 20,
        "matched_character": best_c or user_chars[0],
        "active_character": None,
    }


def correlate_single_flight(
    flight: dict,
    reveals_for_dest: list[dict],
    saved_chars_by_user: dict[str, list[dict]],
    all_saved_chars: list[dict],
    user_alias_map: dict[str, set[str]] | None = None,
    strikes_map: dict[str, int] | None = None,
    active_restr_by_user: dict[str, dict] | None = None,
) -> dict:
    """Correlate a flight against candidate Dodo reveals and saved character slots."""
    f_ign = flight["ign"]
    f_isl = flight["origin_island"]
    f_ts = flight["timestamp"]

    user_alias_map = user_alias_map or {}
    strikes_map = strikes_map or {}
    active_restr_by_user = active_restr_by_user or {}

    candidates: list[dict] = []

    # 1. Search against recent Dodo reveals for this destination island
    for rev in reveals_for_dest:
        rev_user_id = str(rev["user_id"])
        rev_created_at = rev["created_at"]
        time_diff = abs(f_ts - rev_created_at)

        # Look up saved character slots for this revealing user with alias support
        user_chars = get_user_characters_with_aliases(rev_user_id, saved_chars_by_user, user_alias_map)

        eval_res = evaluate_flight_against_user_characters(f_ign, f_isl, user_chars)

        # Also check against Discord nickname if formatted as "IGN | Island" when no saved chars or low score
        if (not user_chars or eval_res["score"] < 50) and rev.get("nickname"):
            parts = [p.strip() for p in rev["nickname"].replace("/", "|").split("|")]
            if len(parts) >= 2:
                n_score, n_ign, n_isl = compare_identity(f_ign, f_isl, parts[0], parts[1])
            elif len(parts) == 1:
                n_ign = fuzz.ratio(normalize_text_for_comparison(f_ign), normalize_text_for_comparison(parts[0])) >= 88
                n_isl = False
                n_score = 50 if n_ign else 0
            else:
                n_score, n_ign, n_isl = 0, False, False

            if n_ign:
                eval_res = {
                    "is_flagged": True,
                    "flag_reason": "no_saved_character",
                    "flag_message": f"Flagged: Discord nickname '{rev['nickname']}' matches, but character is not saved in profile.",
                    "match_status": "partial",
                    "score": min(n_score, 65),
                    "matched_character": {
                        "ign": parts[0],
                        "island_name": parts[1] if len(parts) >= 2 else "",
                        "is_default": False,
                        "icon": "",
                    },
                    "active_character": None,
                }

        matched_char = eval_res.get("matched_character")
        active_char = eval_res.get("active_character")
        shown_char = matched_char or active_char
        _, ign_match, island_match = (
            compare_identity(f_ign, f_isl, shown_char["ign"], shown_char["island_name"])
            if shown_char else (0, False, False)
        )
        strikes = get_user_strike_count(rev_user_id, strikes_map, user_alias_map)
        active_restr = get_user_active_restriction(rev_user_id, active_restr_by_user, user_alias_map)

        candidate_record = {
            "reveal_id": rev["id"],
            "user_id": rev_user_id,
            "username": rev.get("username", ""),
            "nickname": rev.get("nickname", ""),
            "ign": matched_char["ign"] if matched_char else (active_char["ign"] if active_char else ""),
            "island_name": matched_char["island_name"] if matched_char else (active_char["island_name"] if active_char else ""),
            "char_icon": matched_char.get("icon") if matched_char else (active_char.get("icon") if active_char else ""),
            "char_default": bool(matched_char.get("is_default")) if matched_char else (bool(active_char.get("is_default")) if active_char else False),
            "revealed_at": rev_created_at,
            "time_diff_seconds": time_diff,
            "ign_match": ign_match,
            "island_match": island_match,
            "score": eval_res["score"],
            "is_flagged": eval_res["is_flagged"],
            "flag_reason": eval_res["flag_reason"],
            "flag_message": eval_res["flag_message"],
            "match_status": eval_res["match_status"],
            "strikes": strikes,
            "active_restriction": active_restr,
            "channel_id": rev.get("channel_id"),
            "message_url": rev.get("message_url"),
            "matched_character": matched_char,
            "active_character": active_char,
            "all_user_characters": user_chars,
        }
        candidates.append(candidate_record)

    # Sort reveals: unflagged (verified) first, then highest score, then closest in time
    candidates.sort(
        key=lambda c: (
            0 if c["is_flagged"] else 1,
            c["score"],
            -c["time_diff_seconds"],
        ),
        reverse=True,
    )

    best_candidate: dict | None = None
    runner_ups: list[dict] = []

    if candidates:
        best_candidate = candidates[0]
        runner_ups = candidates[1:4]

    # 2. Fallback: If no Dodo reveals matched with score >= 70, check globally across all saved characters
    if (not best_candidate or best_candidate["score"] < 70) and all_saved_chars:
        global_match = None
        global_best_score = 0
        for char in all_saved_chars:
            g_score, g_ign, g_isl = compare_identity(f_ign, f_isl, char["ign"], char["island_name"])
            if g_score > global_best_score:
                global_best_score = g_score
                global_match = (char, g_score, g_ign, g_isl)

        if global_match and global_best_score >= 70:
            char, g_score, g_ign, g_isl = global_match
            is_act = bool(char.get("is_default"))
            if is_act and g_ign and g_isl:
                flag_reason = "missing_dodo_reveal"
                flag_message = f"Flagged: Matched registered active character '{char['ign']}', but NO Dodo code reveal was logged in 40m window."
                m_status = "partial"
                eff_score = 65
            elif not is_act:
                flag_reason = "inactive_character_no_reveal"
                flag_message = f"Flagged: Matched registered inactive slot '{char['ign']}' with no Dodo reveal."
                m_status = "partial"
                eff_score = 50
            else:
                flag_reason = "partial_match_no_reveal"
                flag_message = f"Flagged: Partial match with registered '{char['ign']}', no Dodo reveal."
                m_status = "partial"
                eff_score = 45

            fallback_cand = {
                "reveal_id": None,
                "user_id": str(char["user_id"]),
                "username": "Registered Member",
                "nickname": "",
                "ign": char["ign"],
                "island_name": char["island_name"],
                "char_icon": char.get("icon"),
                "char_default": is_act,
                "revealed_at": None,
                "time_diff_seconds": None,
                "ign_match": g_ign,
                "island_match": g_isl,
                "score": eff_score,
                "is_flagged": True,
                "flag_reason": flag_reason,
                "flag_message": flag_message,
                "match_status": m_status,
                "strikes": get_user_strike_count(char["user_id"], strikes_map, user_alias_map),
                "active_restriction": get_user_active_restriction(char["user_id"], active_restr_by_user, user_alias_map),
                "channel_id": None,
                "message_url": None,
                "matched_character": char,
                "active_character": char if is_act else None,
                "all_user_characters": [char],
            }
            if not best_candidate or eff_score > best_candidate["score"]:
                if best_candidate:
                    runner_ups.insert(0, best_candidate)
                best_candidate = fallback_cand

    is_sub = is_sub_island_destination(flight.get("destination"), flight.get("island_type"))

    # FREE ISLAND FLIGHTS:
    # Public / free islands do not require Dodo reveals on the website or active saved character profiles.
    # Therefore, NEVER flag free island flyers.
    if not is_sub:
        if best_candidate:
            best_candidate["is_flagged"] = False
            best_candidate["flag_reason"] = None
            best_candidate["flag_message"] = None
            best_candidate["match_status"] = "verified"
            eff_score = max(best_candidate.get("score", 0), 95)
            best_candidate["score"] = eff_score
            return {
                "match_status": "verified",
                "match_score": eff_score,
                "is_flagged": False,
                "flag_reason": None,
                "flag_message": None,
                "primary_candidate": best_candidate,
                "runner_ups": runner_ups[:3],
            }
        return {
            "match_status": "verified",
            "match_score": 100,
            "is_flagged": False,
            "flag_reason": None,
            "flag_message": None,
            "primary_candidate": None,
            "runner_ups": [],
        }

    # SUB-ISLAND FLIGHTS:
    # Strictly enforce active character registration & Dodo reveal verification.
    if best_candidate:
        return {
            "match_status": best_candidate["match_status"],
            "match_score": best_candidate["score"],
            "is_flagged": best_candidate["is_flagged"],
            "flag_reason": best_candidate["flag_reason"],
            "flag_message": best_candidate["flag_message"],
            "primary_candidate": best_candidate,
            "runner_ups": runner_ups[:3],
        }

    return {
        "match_status": "unregistered",
        "match_score": 0,
        "is_flagged": True,
        "flag_reason": "unregistered_flight",
        "flag_message": f"Flagged: No Dodo reveal found for '{flight['destination']}' in the 40-minute window.",
        "primary_candidate": None,
        "runner_ups": [],
    }


# -----------------------------------------------------------------------------
# Web auto-check: the flight logger's matching for flights the bot never checked
# -----------------------------------------------------------------------------

# Give the Discord flight logger this long to take over telemetry's row first.
AUTO_CHECK_GRACE_SECONDS = 180
AUTO_CHECK_INTERVAL_SECONDS = 20
# Each flight costs up to ~5 Discord calls (member search, member lookup, post), and the
# shared Discord client spaces every request by >= 0.25s, so a run stays well under limits.
AUTO_CHECK_BATCH = 15
# Older flights still get a verdict, but no Discord post: a backlog must not flood channels.
AUTO_CHECK_ANNOUNCE_MAX_AGE_SECONDS = 3600
_auto_check_lock = threading.Lock()
_auto_check_state = {"last_run": 0.0}


def _search_discord_nickname_matches(ign: str, origin: str) -> list[dict] | None:
    """Members whose "IGN | Island" nickname matches the flight (None if Discord is unreachable)."""
    from utils.discord_http import request as discord_request
    from utils.discord_membership import _bot_headers
    from utils.flight_matching import nickname_matches_flight

    guild_id = str(Config.GUILD_ID or "")
    if not guild_id or not ign:
        return None
    try:
        resp = discord_request(
            f"https://discord.com/api/v10/guilds/{guild_id}/members/search?"
            f"query={urllib.parse.quote(ign[:100])}&limit=50",
            headers=_bot_headers(),
            timeout=8,
        )
        members = json.loads(resp.body) or []
    except Exception as exc:
        logger.debug("[ControlPanel] Discord member search failed for %s: %s", ign, exc)
        return None

    matches = []
    for m in members if isinstance(members, list) else []:
        user = m.get("user") or {}
        display = m.get("nick") or user.get("global_name") or user.get("username") or ""
        if user.get("id") and nickname_matches_flight(display, ign, origin):
            matches.append({"id": str(user["id"]), "name": display})
    return matches


def _match_flight_like_logger(conn, flight: dict) -> tuple[str, dict, str | None]:
    """Run the flight logger's lookup order. Returns (verdict, detail, user_id)."""
    from utils.flight_matching import ign_matches_dodo_reveal

    ign, origin, dest, ts = flight["ign"], flight["origin_island"], flight["destination"], flight["timestamp"]
    found: list[dict] = []
    found_via = None

    # 1. Discord nickname "IGN | Island"
    nick_matches = _search_discord_nickname_matches(ign, origin)
    if nick_matches:
        found, found_via = nick_matches, "discord_nickname"

    # 2. Website saved characters: IGN + island, then IGN only
    if not found:
        owners = saved_character_owners(conn.execute(SAVED_CHARACTERS_SQL).fetchall(), ign, origin)
        if owners:
            found, found_via = [{"id": owners[0], "name": f"{ign} from {origin}"}], "saved_character"

    # 3. Dodo reveal for this island in the last 2 hours
    if not found:
        rows = conn.execute(
            "SELECT user_id, username, nickname FROM dodo_reveal_messages "
            "WHERE island_clean = ? AND created_at BETWEEN ? AND ? ORDER BY created_at DESC LIMIT 40",
            (clean_text(dest), ts - 7200, ts + 300),
        ).fetchall()
        for uid, username, nickname in rows:
            if ign_matches_dodo_reveal(ign, username, nickname):
                found, found_via = [{"id": str(uid), "name": nickname or username or str(uid)}], "dodo_reveal"
                break

    # 4. Recent authorization of this IGN (24h), also used to settle ambiguous matches
    prior = conn.execute(
        "SELECT id, user_id FROM island_visits WHERE ign = ? AND timestamp > ? AND timestamp <= ? "
        "AND authorized = 1 AND user_id IS NOT NULL AND id <> ? ORDER BY timestamp DESC LIMIT 1",
        (ign, ts - 86400, ts, flight["id"]),
    ).fetchone()

    if len(found) > 1:
        settled = next((m for m in found if prior and str(prior[1]) == m["id"]), None)
        if not settled:
            return "ambiguous_match", {
                "reason": f"{len(found)} members matched this flight; manual review required",
                "candidates": found[:15],
                "checked_by": "web",
            }, None
        return "ambiguous_resolved", {
            "reason": f"Several members matched; cleared by previous authorization #{prior[0]}",
            "member": settled,
            "found_via": "recent_authorization",
            "checked_by": "web",
        }, settled["id"]

    if found:
        return "verified", {
            "reason": "Log details matched with a member",
            "member": found[0],
            "found_via": found_via,
            "checked_by": "web",
        }, found[0]["id"]

    if prior:
        return "authorized_followup", {
            "reason": f"Previously verified traveler (matched from visit #{prior[0]})",
            "member": {"id": str(prior[1])},
            "matched_from": prior[0],
            "checked_by": "web",
        }, str(prior[1])

    return "unknown_traveler", {
        "reason": "No matching member found (Discord nickname, saved character, Dodo reveal)",
        "checked_by": "web",
    }, None


def _role_check_like_logger(member_id: str, destination: str) -> tuple[str, dict] | None:
    """The flight logger's role flags for a matched member, using live Discord roles.

    Returns (verdict, detail) when the member should be flagged, else None. The
    logger's third flag (recent nickname change) needs gateway events and is bot-only.
    """
    from datetime import datetime

    from utils import island_access
    from utils.discord_membership import fetch_guild_member_snapshot
    from utils.flight_matching import calculate_max_identities, get_allowed_identity_count

    if not str(member_id).isdigit():
        return None
    try:
        snap = fetch_guild_member_snapshot(str(member_id))
    except Exception as exc:
        logger.debug("[ControlPanel] Role check skipped for %s: %s", member_id, exc)
        return None

    roles = {str(r) for r in snap.get("roles", [])}
    names = island_access.get_guild_role_names()
    sub_roles = set(island_access.ROLE_ID_TO_TIER_NAME)
    member_subs = sorted(roles & sub_roles)
    display_name = snap.get("nickname") or snap.get("global_name") or snap.get("account_name") or ""
    member = {"id": str(member_id), "name": display_name}
    try:
        joined_at = datetime.fromisoformat(str(snap.get("joined_at") or "").replace("Z", "+00:00"))
    except ValueError:
        joined_at = None

    # 1. More nickname identities than subscriptions allow
    identities = calculate_max_identities(display_name)
    allowed = get_allowed_identity_count(joined_at, len(member_subs))
    if identities > allowed:
        return "identity_limit", {
            "reason": f"Nickname has {identities} identities but only {allowed} allowed ({len(member_subs)} subscription(s))",
            "member": member,
            "identities": identities,
            "allowed": allowed,
            "subs": [names.get(r, r) for r in member_subs],
        }

    # 2. No sub, or no tier that opens this island (the island access role alone is not enough)
    required = {
        r for r in island_access.effective_island_required_roles("member", None, "VIP", destination)
        if str(r).isdigit()
    }
    if required and not snap.get("is_mod") and not (set(member_subs) & required):
        have = [names.get(r, r) for r in member_subs]
        need = [names.get(r, r) for r in sorted(required)]
        return "island_access_mismatch", {
            "reason": f"No subscription role for destination island (has: {', '.join(have) or 'none'}; needs: {', '.join(need)})",
            "member": member,
            "subs": have,
            "required_roles": need,
        }
    return None


def _announce_web_verdict(conn, flight: dict, verdict: str, detail: dict, user_id: str | None) -> None:
    """Post what the flight logger would have posted for this verdict (bot was offline)."""
    from utils import flight_discord, island_access

    try:
        member = detail.get("member") or {}
        if LOGGER_VERDICT_KIND.get(verdict) == "verified" and member.get("kind") == "website":
            flight_discord.post_verified_website_flight(flight, member.get("name") or "", detail.get("subs") or [])
        elif LOGGER_VERDICT_KIND.get(verdict) == "verified":
            member_name, has_access, island_subs = member.get("name"), False, []
            if str(user_id or "").isdigit():
                from utils.discord_membership import fetch_guild_member_snapshot
                try:
                    snap = fetch_guild_member_snapshot(str(user_id))
                    roles = {str(r) for r in snap.get("roles", [])}
                    member_name = snap.get("username") or member_name
                    has_access = str(Config.ISLAND_ACCESS_ROLE) in roles
                    required = island_access.effective_island_required_roles("member", None, "VIP", flight["destination"])
                    island_subs = sorted(roles & {str(r) for r in required if str(r).isdigit()})
                except Exception:
                    pass
            flight_discord.post_verified_flight(flight, str(user_id or ""), member_name or "", has_access, island_subs)
        else:
            message = flight_discord.post_flight_alert(flight, verdict, detail)
            if message and message.get("id"):
                # Lets web resolutions close this alert like the logger's own alerts
                conn.execute(
                    "UPDATE island_visits SET alert_channel_id = ?, alert_message_id = ? WHERE id = ?",
                    (str(message.get("channel_id")), str(message["id"]), flight["id"]),
                )
                conn.commit()
    except Exception as exc:
        logger.warning("[ControlPanel] Could not post web verdict for visit #%s: %s", flight["id"], exc)


def auto_check_unchecked_visits(force: bool = False, only_ids: list[int] | None = None) -> int:
    """Authorise or flag sub flights the Discord flight logger never checked.

    Uses the logger's own matching order and its role flags (identity limit, island
    access). Only the "recent nickname change" flag needs the bot's gateway events.
    """
    now = time.time()
    if not force and now - _auto_check_state["last_run"] < AUTO_CHECK_INTERVAL_SECONDS:
        return 0
    if not _auto_check_lock.acquire(blocking=False):
        return 0
    try:
        _auto_check_state["last_run"] = now
        sub_sql, sub_params = sub_destination_filter()
        id_sql = f" AND v.id IN ({','.join('?' for _ in only_ids)})" if only_ids else ""
        conn = connect_db()
        try:
            rows = conn.execute(
                f"""
                SELECT v.id, v.ign, v.origin_island, v.destination, v.timestamp
                FROM island_visits v
                WHERE v.review_status = 'unchecked' AND {sub_sql} AND v.timestamp <= ?{id_sql}
                ORDER BY v.id ASC LIMIT ?
                """,
                (*sub_params, int(now) - AUTO_CHECK_GRACE_SECONDS, *(only_ids or []), AUTO_CHECK_BATCH),
            ).fetchall()
            checked = 0
            for r in rows:
                flight = {"id": r[0], "ign": r[1] or "", "origin_island": r[2] or "", "destination": r[3] or "", "timestamp": r[4] or 0}
                verdict, detail, user_id = _match_flight_like_logger(conn, flight)
                # Saved characters and Dodo reveals can belong to accounts outside Discord:
                # their website subscription decides, like it did for the Dodo reveal.
                account = (
                    website_only_account(user_id)
                    if verdict == "verified" and detail.get("found_via") in ("saved_character", "dodo_reveal")
                    else None
                )
                if account:
                    verdict, detail = website_verdict(account, flight["destination"], detail.get("found_via"))
                    detail["checked_by"] = "web"
                    user_id = None
                elif user_id and verdict in ("verified", "ambiguous_resolved"):
                    flagged = _role_check_like_logger(user_id, flight["destination"])
                    if flagged:
                        verdict, extra = flagged
                        detail = {**detail, **extra, "found_via": detail.get("found_via")}
                authorised = LOGGER_VERDICT_KIND.get(verdict) == "verified"
                conn.execute(
                    """
                    UPDATE island_visits
                    SET review_status = ?, authorized = ?, user_id = COALESCE(?, user_id),
                        logger_verdict = ?, logger_detail = ?
                    WHERE id = ? AND review_status = 'unchecked'
                    """,
                    (
                        "authorised" if authorised else "pending",
                        # Like the logger: a matched member keeps authorized = 1 even when
                        # flagged, so their next flights count as previously verified.
                        1 if user_id or (account and authorised) else 0,
                        int(user_id) if user_id and str(user_id).isdigit() else None,
                        verdict,
                        json.dumps(detail),
                        flight["id"],
                    ),
                )
                conn.commit()
                if now - flight["timestamp"] <= AUTO_CHECK_ANNOUNCE_MAX_AGE_SECONDS:
                    _announce_web_verdict(conn, flight, verdict, detail, user_id)
                checked += 1
            conn.commit()
            return checked
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("[ControlPanel] Auto-check of unchecked flights failed: %s", exc)
        return 0
    finally:
        _auto_check_lock.release()


def kick_auto_check() -> None:
    """Run auto_check_unchecked_visits in the background (Discord lookups can be slow)."""
    if time.time() - _auto_check_state["last_run"] >= AUTO_CHECK_INTERVAL_SECONDS:
        threading.Thread(target=auto_check_unchecked_visits, daemon=True).start()


# -----------------------------------------------------------------------------
# Endpoints
# -----------------------------------------------------------------------------

@control_panel_bp.route("/live-feed", methods=["GET"])
@require_mod_auth
def get_live_feed():
    """Fetch the live feed of Sub-Island flights with the Discord flight logger's verdicts.

    The verdict (verified / unknown / ambiguous / flagged) is the one bots/flight_logger.py
    stored; the website's own Dodo-reveal/saved-character check is returned separately as
    ``website_check`` for investigation only. Free-island flights are excluded, and pending
    re-joins of the same IGN to the same island are merged into one item.
    """
    init_control_panel_db()
    kick_auto_check()

    limit = max(1, min(int(request.args.get("limit", 50) or 50), 500))
    since_id = request.args.get("since_id")
    island_filter = request.args.get("island")
    status_filter = request.args.get("status", "all").lower().strip()
    search_query = request.args.get("search", "").strip().lower()

    conn = connect_db()
    try:
        # Build flights query
        sub_sql, sub_params = sub_destination_filter()
        where_clauses = [sub_sql]
        params: list[Any] = list(sub_params)

        if since_id and since_id.isdigit():
            where_clauses.append("v.id > ?")
            params.append(int(since_id))

        if island_filter:
            where_clauses.append("LOWER(v.destination) = LOWER(?)")
            params.append(island_filter.strip())

        if status_filter == "pending":
            where_clauses.append("v.review_status = 'pending'")
        elif status_filter == "alerts":
            where_clauses.append(f"v.logger_verdict IN ({','.join('?' for _ in LOGGER_FLAG_VERDICTS)})")
            params.extend(LOGGER_FLAG_VERDICTS)
        elif status_filter == "unchecked":
            where_clauses.append(UNCHECKED_SQL)
        elif status_filter == "authorised":
            where_clauses.append("v.review_status = 'authorised'")
        elif status_filter == "dismissed":
            where_clauses.append("v.review_status = 'dismissed'")
        elif status_filter == "actioned":
            where_clauses.append("v.review_status = 'actioned'")

        where_sql = " AND ".join(where_clauses)
        cur = conn.execute(
            f"""
            SELECT v.id, v.ign, v.origin_island, v.destination, v.user_id, v.guild_id,
                   v.authorized, v.timestamp, v.island_type, v.review_status,
                   v.handled_by, v.handled_at, v.match_score, v.correlated_reveal_id,
                   v.logger_verdict, v.logger_detail
            FROM island_visits v
            WHERE {where_sql}
            ORDER BY v.id DESC
            LIMIT ?
            """,
            tuple(params + [limit]),
        )
        flight_rows = cur.fetchall()

        if not flight_rows:
            return jsonify({
                "flights": [],
                "count": 0,
                "has_more": False,
                "sub_islands": sorted(get_sub_island_names()),
                "server_time": int(time.time()),
            })

        # Strike counts (active warnings) for the strike badge
        cur = conn.execute(
            """
            SELECT user_id, COUNT(*)
            FROM warnings
            WHERE action_type IN ('WARN', 'TIMEOUT', 'BAN')
            GROUP BY user_id
            """
        )
        strikes_map = {str(row[0]): row[1] for row in cur.fetchall()}
        strikes_map.update(_web_strikes(conn))

        # The website's own Dodo-reveal / saved-character match is not run here: it only
        # feeds Investigate (GET /flight/<id>/investigate), so 8s refreshes stay cheap.
        feed_items = []
        pending_by_key: dict[tuple[str, str], dict] = {}
        for row in flight_rows:
            flight = {
                "id": row[0],
                "ign": row[1],
                "origin_island": row[2],
                "destination": row[3],
                "user_id": row[4],
                "guild_id": row[5],
                "authorized": row[6],
                "timestamp": row[7],
                "island_type": row[8],
                "review_status": row[9] or "pending",
                "handled_by": row[10],
                "handled_at": row[11],
                "persisted_match_score": row[12],
                "correlated_reveal_id": row[13],
                "logger_verdict": row[14],
                "logger_detail": row[15],
            }

            dest_key = clean_text(flight["destination"]).lower()

            # Determine whether this flight is to an official Sub-Island or Free Island
            is_sub = is_sub_island_destination(flight["destination"], flight["island_type"])
            if not is_sub:
                if flight["island_type"] != "free":
                    try:
                        conn.execute("UPDATE island_visits SET island_type = 'free' WHERE id = ?", (flight["id"],))
                        conn.commit()
                    except Exception:
                        pass
                continue

            # Merge pending re-joins into the newest alert (rows are newest first)
            merge_key = (clean_text(flight["ign"]).lower(), dest_key)
            if flight["review_status"] == "pending":
                newer = pending_by_key.get(merge_key)
                if newer and newer["first_seen"] - flight["timestamp"] <= REJOIN_MERGE_SECONDS:
                    newer["attempts"] += 1
                    newer["merged_ids"].append(flight["id"])
                    newer["first_seen"] = flight["timestamp"]
                    continue

            alert_kind, detail = classify_logger_verdict(flight)
            logger_member = detail.get("member") or (
                {"id": str(flight["user_id"])} if flight["user_id"] else None
            )
            logger_candidates = detail.get("candidates") or []

            member_id = str((logger_member or {}).get("id") or "")
            strike_count = strikes_map.get(member_id, 0) if member_id else 0

            item = {
                "id": flight["id"],
                "ign": flight["ign"],
                "origin_island": flight["origin_island"],
                "destination": flight["destination"],
                "timestamp": flight["timestamp"],
                "island_type": flight["island_type"],
                "review_status": flight["review_status"],
                "handled_by": flight["handled_by"],
                "handled_at": flight["handled_at"],
                "alert_kind": alert_kind,
                "is_flagged": alert_kind in ("unknown", "ambiguous", "flagged"),
                "verdict": flight["logger_verdict"],
                "verdict_reason": detail.get("reason"),
                "verdict_detail": detail,
                "found_via": detail.get("found_via"),
                "logger_member": logger_member,
                "logger_candidates": logger_candidates,
                "strike_count": strike_count,
                "attempts": 1,
                "merged_ids": [],
                "first_seen": flight["timestamp"],
            }

            # Optional search filtering
            if search_query:
                names = [(logger_member or {}).get("name") or "", *(c.get("name") or "" for c in logger_candidates)]
                combined_text = (
                    f"{flight['ign']} {flight['origin_island']} {flight['destination']} {' '.join(names)}"
                ).lower()
                if search_query not in combined_text:
                    continue

            feed_items.append(item)
            if flight["review_status"] == "pending":
                pending_by_key.setdefault(merge_key, item)

        # What reached Discord for the latest web action on each flight
        if feed_items:
            ph = ",".join("?" for _ in feed_items)
            latest_sync: dict[int, dict] = {}
            for r in conn.execute(
                f"""
                SELECT visit_id, action, case_id, dm_status, case_log_url, case_log_status, alert_status, created_at
                FROM control_panel_discord_sync WHERE visit_id IN ({ph}) ORDER BY id
                """,
                [i["id"] for i in feed_items],
            ).fetchall():
                latest_sync[r[0]] = {
                    "action": r[1], "case_id": r[2], "dm": r[3], "case_log_url": r[4],
                    "case_log": r[5], "alert": r[6], "at": r[7],
                }
            for item in feed_items:
                item["discord_sync"] = latest_sync.get(item["id"])

        return jsonify({
            "flights": feed_items,
            "count": len(feed_items),
            "has_more": len(flight_rows) == limit,
            "sub_islands": sorted(get_sub_island_names()),
            "server_time": int(time.time()),
        })
    finally:
        conn.close()


def _website_check(conn, flight: dict) -> dict:
    """The website's own Dodo-reveal / saved-character match for one flight."""
    ts = int(flight["timestamp"] or 0)
    dest_key = clean_text(flight["destination"] or "").lower()
    reveals = [
        {
            "id": r[0], "user_id": r[1], "island_clean": r[2], "channel_id": r[3],
            "message_url": r[4], "username": r[5], "nickname": r[6], "created_at": r[7],
        }
        for r in conn.execute(
            """
            SELECT id, user_id, island_clean, channel_id, message_url, username, nickname, created_at
            FROM dodo_reveal_messages WHERE created_at BETWEEN ? AND ? ORDER BY created_at DESC
            """,
            (ts - 2400, ts + 300),  # 40 min before arrival to 5 min after
        ).fetchall()
        if clean_text(r[2] or "").lower() == dest_key
    ]

    all_saved_chars: list[dict] = []
    saved_chars_by_user: dict[str, list[dict]] = {}
    for c in conn.execute("SELECT user_id, id, ign, island_name, icon, is_default FROM user_saved_characters").fetchall():
        c_dict = {"user_id": c[0], "id": c[1], "ign": c[2], "island_name": c[3], "icon": c[4], "is_default": bool(c[5])}
        all_saved_chars.append(c_dict)
        saved_chars_by_user.setdefault(c[0], []).append(c_dict)

    strikes_map = {
        str(r[0]): r[1]
        for r in conn.execute(
            "SELECT user_id, COUNT(*) FROM warnings WHERE action_type IN ('WARN', 'TIMEOUT', 'BAN') GROUP BY user_id"
        ).fetchall()
    }
    strikes_map.update(_web_strikes(conn))
    active_restr_by_user: dict[str, dict] = {}
    for ar in conn.execute(
        """
        SELECT id, user_id, discord_id, action_type, reason, created_at, expires_at
        FROM user_moderation_status
        WHERE is_active = 1 AND (expires_at IS NULL OR expires_at > ?)
        ORDER BY created_at DESC
        """,
        (int(time.time()),),
    ).fetchall():
        restr = {"id": ar[0], "user_id": ar[1], "discord_id": ar[2], "action_type": ar[3],
                 "reason": ar[4], "created_at": ar[5], "expires_at": ar[6]}
        for key in (ar[1], ar[2]):
            if key:
                active_restr_by_user.setdefault(str(key), restr)

    user_alias_map: dict[str, set[str]] = {}
    try:
        for prow in conn.execute("SELECT provider_user_id, unified_user_id FROM user_provider_links").fetchall():
            p_uid, u_uid = str(prow[0] or "").strip(), str(prow[1] or "").strip()
            if p_uid and u_uid:
                user_alias_map.setdefault(p_uid, set()).add(u_uid)
                user_alias_map.setdefault(u_uid, set()).add(p_uid)
    except Exception:
        pass

    correlation = correlate_single_flight(
        flight, reveals, saved_chars_by_user, all_saved_chars, user_alias_map, strikes_map, active_restr_by_user
    )
    return {
        "match_status": correlation["match_status"],
        "match_score": correlation["match_score"],
        "is_flagged": correlation["is_flagged"],
        "flag_message": correlation["flag_message"],
        "primary_candidate": correlation["primary_candidate"],
        "runner_ups": correlation["runner_ups"],
    }


# Warnings-table action types -> how the timeline describes them
_TIMELINE_ACTIONS = {
    "ADMIT": "Admitted",
    "DISMISS": "Dismissed",
    "FLAG": "Flagged",
    "NOTE": "Note",
    "WARN": "Warned",
    "TIMEOUT": "Timed out",
    "BAN": "Banned",
    "KICK": "Kicked",
    "R1": "Rule 1 warning",
}


def _flight_timeline(conn, visit: dict, visit_ids: list[int]) -> list[dict]:
    """Everything that happened to a flight, from Discord and the web, oldest first."""
    events: list[dict] = [{
        "at": visit["timestamp"],
        "source": "SysBot",
        "actor": None,
        "action": "Arrived",
        "text": f"{visit['ign']} from {visit['origin_island'] or '?'} joined {visit['destination']}",
    }]
    if visit.get("logger_verdict"):
        kind, detail = classify_logger_verdict(visit)
        events.append({
            "at": visit["timestamp"],
            "source": "Website" if detail.get("checked_by") == "web" else "Flight logger",
            "actor": None,
            "action": {"verified": "Verified", "unknown": "Unknown traveler", "ambiguous": "Ambiguous match"}.get(kind, "Flagged"),
            "text": detail.get("reason") or visit["logger_verdict"],
        })

    ph = ",".join("?" for _ in visit_ids)
    # Discord buttons and web notes/strikes both land in `warnings`. Web rows carry a
    # mod_name; Discord rows only have the moderator's ID.
    for w in conn.execute(
        f"SELECT timestamp, action_type, reason, mod_id, mod_name, user_id FROM warnings "
        f"WHERE visit_id IN ({ph}) AND timestamp >= ? ORDER BY timestamp",
        [*visit_ids, visit["timestamp"] - REJOIN_MERGE_SECONDS],
    ).fetchall():
        action_type = str(w[1] or "WARN").upper()
        target = f" · member {w[5]}" if w[5] and str(w[5]) != "0" else ""
        events.append({
            "at": w[0],
            "source": "Web" if w[4] else "Discord",
            "actor": w[4] or (f"<@{w[3]}>" if w[3] else None),
            "action": _TIMELINE_ACTIONS.get(action_type, action_type.title()),
            "text": (w[2] or "") + target,
        })

    # Web authorise/dismiss/undo have no warnings row; they are in the audit log.
    targets = [f"visit:{i}" for i in visit_ids]
    tph = ",".join("?" for _ in targets)
    audit_labels = {
        "CONTROL_PANEL_AUTHORISE": "Admitted",
        "CONTROL_PANEL_DISMISS": "Dismissed",
        "CONTROL_PANEL_ACTION_REVERSED": "Reopened (undo)",
    }
    for a in conn.execute(
        f"SELECT created_at, actor_name, action, details FROM dashboard_audit_events WHERE target IN ({tph}) "
        f"AND action IN ('CONTROL_PANEL_AUTHORISE', 'CONTROL_PANEL_DISMISS', 'CONTROL_PANEL_ACTION_REVERSED') "
        # Visit IDs can be reused after deletes; ignore records from before this flight
        f"AND created_at >= ?",
        [*targets, visit["timestamp"] - REJOIN_MERGE_SECONDS],
    ).fetchall():
        try:
            details = json.loads(a[3] or "{}")
        except (TypeError, ValueError):
            details = {}
        member = details.get("target_discord_id") or details.get("target_user_id")
        events.append({
            "at": a[0],
            "source": "Web",
            "actor": a[1],
            "action": audit_labels[a[2]],
            "text": f"member {member}" if member else "",
        })

    # Resolutions made with Discord buttons are only recorded on the visit itself
    handled_by = visit.get("handled_by") or ""
    if handled_by.endswith("(Discord)") and visit.get("handled_at"):
        events.append({
            "at": visit["handled_at"],
            "source": "Discord",
            "actor": handled_by.removesuffix(" (Discord)"),
            "action": {"authorised": "Admitted", "dismissed": "Dismissed"}.get(visit["review_status"], "Actioned"),
            "text": "",
        })

    events.sort(key=lambda e: (e["at"] or 0))
    return events


@control_panel_bp.route("/reasons", methods=["GET"])
@require_mod_auth
def list_reason_templates():
    """The flight logger's punishment reasons (same texts as its Discord reason picker)."""
    from utils.flight_discord import REASON_LABELS, REASON_TEMPLATES

    return jsonify({
        "reasons": [
            {"key": key, "label": REASON_LABELS.get(key, key), "text": text}
            for key, text in REASON_TEMPLATES.items()
        ],
    })


@control_panel_bp.route("/members/search", methods=["GET"])
@require_mod_auth
def search_members():
    """Search server members by name/nickname for the action form's member picker."""
    from utils.discord_http import request as discord_request
    from utils.discord_membership import _bot_headers

    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify({"members": []})
    if q.isdigit() and len(q) >= 15:
        # A pasted Discord ID: look it up directly
        from utils.discord_membership import fetch_guild_member_snapshot
        try:
            snap = fetch_guild_member_snapshot(q)
            return jsonify({"members": [{
                "id": q,
                "name": snap.get("username"),
                "username": snap.get("account_name"),
                "avatar": snap.get("avatar"),
                "has_island_access": str(Config.ISLAND_ACCESS_ROLE) in snap.get("roles", []),
            }]})
        except Exception:
            return jsonify({"members": []})
    try:
        resp = discord_request(
            f"https://discord.com/api/v10/guilds/{Config.GUILD_ID}/members/search?"
            f"query={urllib.parse.quote(q[:100])}&limit=25",
            headers=_bot_headers(),
            timeout=8,
        )
        raw = json.loads(resp.body) or []
    except Exception as exc:
        logger.warning("[ControlPanel] Member search failed: %s", exc)
        return jsonify({"error": "Discord member search is unavailable"}), 502

    members = []
    for m in raw if isinstance(raw, list) else []:
        u = m.get("user") or {}
        if not u.get("id") or u.get("bot"):
            continue
        avatar = f"https://cdn.discordapp.com/avatars/{u['id']}/{u['avatar']}.png?size=64" if u.get("avatar") else None
        members.append({
            "id": str(u["id"]),
            "name": m.get("nick") or u.get("global_name") or u.get("username"),
            "username": u.get("username"),
            "avatar": avatar,
            "has_island_access": str(Config.ISLAND_ACCESS_ROLE) in {str(r) for r in m.get("roles", [])},
        })
    return jsonify({"members": members})


@control_panel_bp.route("/flight/<int:visit_id>/investigate", methods=["GET"])
@require_mod_auth
def investigate_flight(visit_id: int):
    """Investigate data for one flight: the website's match and the full timeline.

    ``merged`` (comma-separated visit IDs) adds re-join attempts merged into this alert.
    """
    merged = [int(i) for i in request.args.get("merged", "").split(",") if i.strip().isdigit()]
    conn = connect_db()
    try:
        row = conn.execute(
            """
            SELECT id, ign, origin_island, destination, user_id, guild_id, authorized, timestamp,
                   island_type, review_status, handled_by, handled_at, logger_verdict, logger_detail
            FROM island_visits WHERE id = ?
            """,
            (visit_id,),
        ).fetchone()
        if not row:
            return jsonify({"error": "Flight visit not found"}), 404
        visit = {
            "id": row[0], "ign": row[1] or "", "origin_island": row[2] or "", "destination": row[3] or "",
            "user_id": row[4], "guild_id": row[5], "authorized": row[6], "timestamp": row[7] or 0,
            "island_type": row[8], "review_status": row[9], "handled_by": row[10], "handled_at": row[11],
            "logger_verdict": row[12], "logger_detail": row[13],
        }
        return jsonify({
            "website_check": _website_check(conn, visit),
            "timeline": _flight_timeline(conn, visit, [visit_id, *merged]),
        })
    finally:
        conn.close()


@control_panel_bp.route("/stats", methods=["GET"])
@require_mod_auth
def get_stats():
    """Return live KPI stats for the Control Panel top bar."""
    now = int(time.time())
    one_day_ago = now - 86400

    sub_sql, sub_params = sub_destination_filter()
    conn = connect_db()
    try:
        # Flights today
        cur = conn.execute(
            f"SELECT COUNT(*) FROM island_visits v WHERE {sub_sql} AND v.timestamp >= ?",
            (*sub_params, one_day_ago),
        )
        flights_today = cur.fetchone()[0]

        # Pending count
        cur = conn.execute(
            f"SELECT COUNT(*) FROM island_visits v WHERE {sub_sql} AND v.review_status = 'pending'",
            sub_params,
        )
        pending_total = cur.fetchone()[0]

        # Authorised today
        cur = conn.execute(
            f"SELECT COUNT(*) FROM island_visits v WHERE {sub_sql} AND v.review_status = 'authorised' AND v.handled_at >= ?",
            (*sub_params, one_day_ago),
        )
        authorised_today = cur.fetchone()[0]

        # Actioned today
        cur = conn.execute(
            f"SELECT COUNT(*) FROM island_visits v WHERE {sub_sql} AND v.review_status = 'actioned' AND v.handled_at >= ?",
            (*sub_params, one_day_ago),
        )
        actioned_today = cur.fetchone()[0]

        # Sub flights the flight logger never checked (bot offline)
        cur = conn.execute(
            f"SELECT COUNT(*) FROM island_visits v WHERE {sub_sql} AND {UNCHECKED_SQL} AND v.timestamp >= ?",
            (*sub_params, one_day_ago),
        )
        unchecked_today = cur.fetchone()[0]

        # Active restrictions count
        cur = conn.execute(
            "SELECT COUNT(*) FROM user_moderation_status WHERE is_active = 1 AND (expires_at IS NULL OR expires_at > ?)",
            (now,),
        )
        active_restrictions = cur.fetchone()[0]

        return jsonify({
            "flights_today": flights_today,
            "pending_total": pending_total,
            "authorised_today": authorised_today,
            "actioned_today": actioned_today,
            "active_restrictions": active_restrictions,
            "unchecked_today": unchecked_today,
            "server_time": now,
        })
    finally:
        conn.close()


@control_panel_bp.route("/action", methods=["POST"])
@require_mod_auth
def execute_action():
    """Execute moderation action on a flight visit with concurrency locking and idempotency."""
    init_control_panel_db()

    # Idempotency check
    idempotency_key = request.headers.get("X-Idempotency-Key", "").strip()
    conn = connect_db()
    try:
        if idempotency_key:
            cur = conn.execute("SELECT response_json FROM control_panel_idempotency WHERE `key` = ?", (idempotency_key,))
            existing = cur.fetchone()
            if existing:
                return jsonify(json.loads(existing[0]))

        data = request.get_json(silent=True) or {}
        action = str(data.get("action", "")).lower().strip()
        visit_id = data.get("visit_id")
        user_id = str(data.get("user_id") or "").strip()
        discord_id = str(data.get("discord_id") or "").strip()
        reason = str(data.get("reason", "")).strip() or "Moderator action"
        notes = str(data.get("notes", "")).strip()
        duration_hours = int(data.get("duration_hours") or 0)
        force = bool(data.get("force", False))

        if not action or not visit_id:
            return jsonify({"error": "Missing required fields: action, visit_id"}), 400

        valid_actions = {"authorise", "dismiss", "warn", "timeout", "ban", "note"}
        if action not in valid_actions:
            return jsonify({"error": f"Invalid action: {action}. Must be one of {valid_actions}"}), 400
        merged_ids = [int(i) for i in (data.get("merged_ids") or []) if str(i).isdigit()]

        # A website account outside the Discord server (e.g. cp_usr_…) has no Discord ID
        web_user_id = next((v for v in (user_id, discord_id) if v and not v.isdigit()), "")
        if not discord_id.isdigit():
            discord_id = ""

        user = _get_current_user() or {}
        mod_name = user.get("username") or user.get("user_id") or "Moderator"
        mod_id = str(user.get("user_id") or "system")
        # NULL rather than 0 when there is no Discord ID: MySQL compares 'cp_usr_…' = 0 as true
        warn_user_id = int(discord_id) if discord_id else (int(user_id) if user_id.isdigit() else None)
        warn_mod_id = int(user.get("discord_id")) if str(user.get("discord_id") or "").isdigit() else (int(mod_id) if str(mod_id).isdigit() else 0)
        now = int(time.time())

        def add_warning_row(action_type: str) -> None:
            conn.execute(
                "INSERT INTO warnings (user_id, guild_id, reason, mod_id, timestamp, visit_id, action_type, mod_name, web_user_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (warn_user_id, Config.GUILD_ID or 0, reason, warn_mod_id, now, visit_id, action_type, mod_name, web_user_id or None),
            )

        # Check optimistic concurrency
        cur = conn.execute(
            "SELECT review_status, handled_by, destination, ign, origin_island, alert_channel_id, alert_message_id, "
            "handled_at FROM island_visits WHERE id = ?",
            (visit_id,),
        )
        visit_row = cur.fetchone()
        if not visit_row:
            return jsonify({"error": "Flight record not found"}), 404

        (current_status, handled_by, dest_island, flight_ign, flight_origin,
         alert_channel, alert_message, prev_handled_at) = visit_row

        # Notes never change the review state, so they can be added to any flight.
        if action == "note":
            if not str(data.get("reason") or "").strip():
                return jsonify({"error": "Note text is required"}), 400
            add_warning_row('NOTE')
            conn.commit()
            return jsonify({
                "success": True,
                "action": action,
                "visit_id": visit_id,
                "review_status": current_status,
                "message": "Note added.",
            })

        if current_status not in ("pending", "unchecked") and not force:
            return jsonify({
                "error": f"Flight was already handled by {handled_by or 'another moderator'} ({current_status}).",
                "code": "already_handled",
                "handled_by": handled_by,
                "current_status": current_status,
            }), 409

        # Claim the flight atomically: only one request (double click, or two mods at once)
        # can move it from the state we just read. The loser gets "already handled".
        claim = conn.execute(
            "UPDATE island_visits SET handled_by = ?, handled_at = ? "
            "WHERE id = ? AND review_status = ? AND COALESCE(handled_at, 0) = ?",
            (mod_name, now, visit_id, current_status, prev_handled_at or 0),
        )
        if claim.rowcount != 1:
            conn.rollback()
            latest = conn.execute("SELECT review_status, handled_by FROM island_visits WHERE id = ?", (visit_id,)).fetchone()
            return jsonify({
                "error": f"Flight was just handled by {(latest[1] if latest else None) or 'another moderator'}.",
                "code": "already_handled",
                "handled_by": latest[1] if latest else None,
                "current_status": latest[0] if latest else None,
            }), 409

        # Handle specific actions
        status_to_set = "actioned"
        expires_at = None

        if action == "authorise":
            status_to_set = "authorised"
            # Link the admitted member so later flights by this IGN auto-clear,
            # like the Discord logger's Confirm/Admit flow.
            conn.execute(
                "UPDATE island_visits SET review_status = 'authorised', authorized = 1, "
                "user_id = COALESCE(?, user_id), handled_by = ?, handled_at = ? WHERE id = ?",
                (warn_user_id or None, mod_name, now, visit_id),
            )
        elif action == "dismiss":
            status_to_set = "dismissed"
            conn.execute(
                "UPDATE island_visits SET review_status = 'dismissed', handled_by = ?, handled_at = ? WHERE id = ?",
                (mod_name, now, visit_id),
            )
        elif action == "warn":
            status_to_set = "actioned"
            conn.execute(
                "UPDATE island_visits SET review_status = 'actioned', handled_by = ?, handled_at = ? WHERE id = ?",
                (mod_name, now, visit_id),
            )
            add_warning_row('WARN')
        elif action == "timeout":
            status_to_set = "actioned"
            duration_hours = max(1, duration_hours or 24)
            expires_at = now + (duration_hours * 3600)

            conn.execute(
                "UPDATE island_visits SET review_status = 'actioned', handled_by = ?, handled_at = ? WHERE id = ?",
                (mod_name, now, visit_id),
            )
            add_warning_row('TIMEOUT')
            conn.execute(
                """
                INSERT INTO user_moderation_status
                (user_id, discord_id, action_type, reason, mod_id, mod_name, created_at, expires_at, is_active)
                VALUES (?, ?, 'TIMEOUT', ?, ?, ?, ?, ?, 1)
                """,
                (user_id or discord_id or flight_ign, discord_id or None, reason, mod_id, mod_name, now, expires_at),
            )
        elif action == "ban":
            # Moderators and admins can ban (require_mod_auth already checked the role)
            status_to_set = "actioned"
            conn.execute(
                "UPDATE island_visits SET review_status = 'actioned', authorized = 0, has_island_access = 0, handled_by = ?, handled_at = ? WHERE id = ?",
                (mod_name, now, visit_id),
            )
            add_warning_row('BAN')
            conn.execute(
                """
                INSERT INTO user_moderation_status
                (user_id, discord_id, action_type, reason, mod_id, mod_name, created_at, expires_at, is_active)
                VALUES (?, ?, 'BAN', ?, ?, ?, ?, NULL, 1)
                """,
                (user_id or discord_id or flight_ign, discord_id or None, reason, mod_id, mod_name, now),
            )

        # Resolve merged re-join attempts together with the alert they belong to
        if merged_ids:
            placeholders = ",".join("?" for _ in merged_ids)
            conn.execute(
                f"""
                UPDATE island_visits
                SET review_status = ?, handled_by = ?, handled_at = ?
                WHERE id IN ({placeholders}) AND review_status = 'pending'
                  AND LOWER(ign) = LOWER(?) AND LOWER(destination) = LOWER(?)
                """,
                [status_to_set, mod_name, now, *merged_ids, flight_ign, dest_island],
            )

        revoked_access = None
        case_id = None
        if action in ("warn", "timeout", "ban"):
            from utils.flight_discord import make_case_id
            case_id = make_case_id()
            target_to_revoke = warn_user_id or user_id or discord_id
            if target_to_revoke:
                try:
                    from utils.discord_membership import revoke_sub_rules_acceptance, snapshot_sub_rules_acceptance
                    # Saved in the audit record so Undo can give the access back
                    revoked_access = snapshot_sub_rules_acceptance(target_to_revoke)
                    revoke_sub_rules_acceptance(target_to_revoke)
                except Exception as rev_err:
                    logger.warning(f"Failed to revoke sub rules in control panel for {target_to_revoke}: {rev_err}")

        # Audit event record
        audit_details = {
            "visit_id": visit_id,
            "flight_ign": flight_ign,
            "flight_origin": flight_origin,
            "destination": dest_island,
            "target_user_id": user_id,
            "target_discord_id": discord_id,
            "reason": reason,
            "notes": notes,
            "duration_hours": duration_hours,
            "expires_at": expires_at,
            "revoked_access": revoked_access,
            "case_id": case_id,
        }
        try:
            conn.execute(
                """
                INSERT INTO dashboard_audit_events
                (actor_user_id, actor_name, action, target, details, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    mod_id,
                    mod_name,
                    f"CONTROL_PANEL_{action.upper()}",
                    f"visit:{visit_id}",
                    json.dumps(audit_details),
                    now,
                ),
            )
        except Exception:
            pass

        if not alert_message and merged_ids:
            placeholders = ",".join("?" for _ in merged_ids)
            row = conn.execute(
                f"SELECT alert_channel_id, alert_message_id FROM island_visits "
                f"WHERE id IN ({placeholders}) AND alert_message_id IS NOT NULL LIMIT 1",
                merged_ids,
            ).fetchone()
            if row:
                alert_channel, alert_message = row[0], row[1]

        conn.commit()

        # Resolve the member's Discord ID (web accounts may be linked through a unified ID)
        target_discord_id = str(discord_id or "").strip()
        if not target_discord_id and user_id:
            try:
                from utils.unified_auth import get_unified_account_by_id
                acc = get_unified_account_by_id(user_id)
                if acc:
                    target_discord_id = str(acc.get("discord_id") or acc.get("connected_accounts", {}).get("discord", {}).get("id") or "").strip()
            except Exception:
                pass
        if not target_discord_id and str(user_id or "").isdigit():
            target_discord_id = str(user_id).strip()

        # Mirror the action into Discord #xlog-verbose (non-blocking)
        try:
            from utils.xlog_sync import post_xlog_event_async
            post_xlog_event_async(
                action,
                mod_name,
                reason,
                discord_id=target_discord_id or None,
                user_id=user_id or None,
                ign=flight_ign,
                destination=dest_island,
                origin=flight_origin,
                visit_id=visit_id,
                expires_at=expires_at,
                role_stripped=action in ("warn", "timeout", "ban"),
            )
        except Exception as xlog_err:
            logger.warning("[ControlPanel] Failed to dispatch XLog sync: %s", xlog_err)

        # Same Discord messages as the flight logger: DM + case log for punishments, and the
        # #flight-logs alert closed ("CASE CLOSED" + "Action Taken") so nobody handles it twice.
        from utils.flight_discord import warn_count as _warn_count

        # Members outside the server get no DM; the case log and alert name their website account
        web_account = website_only_account(web_user_id) if web_user_id and not target_discord_id else None
        threading.Thread(
            target=_sync_action_to_discord,
            kwargs={
                "visit_id": visit_id,
                "action": action,
                "mod_name": mod_name,
                "reason": reason,
                "case_id": case_id,
                "discord_id": target_discord_id,
                "alert_channel": alert_channel,
                "alert_message": alert_message,
                "expires_at": expires_at,
                "warn_count": _warn_count(conn, target_discord_id, web_user_id=web_user_id) if case_id else 0,
                "web_user_id": web_user_id if web_account else None,
                "web_name": account_label(web_account) if web_account else None,
            },
            daemon=True,
        ).start()

        response_payload = {
            "success": True,
            "action": action,
            "visit_id": visit_id,
            "review_status": status_to_set,
            "handled_by": mod_name,
            "handled_at": now,
            "expires_at": expires_at,
            "case_id": case_id,
            "message": f"Action '{action}' applied successfully." + (f" Case {case_id}." if case_id else ""),
        }

        # Cache idempotency
        if idempotency_key:
            conn.execute(
                "INSERT OR REPLACE INTO control_panel_idempotency (`key`, response_json, created_at) VALUES (?, ?, ?)",
                (idempotency_key, json.dumps(response_payload), now),
            )
            conn.commit()

        return jsonify(response_payload)
    finally:
        conn.close()


def _record_discord_sync(visit_id, action: str, case_id: str | None, dm_status: str | None,
                         case_log_url: str | None, case_log_status: str | None, alert_status: str | None) -> None:
    """Remember what reached Discord for a web action, so the dashboard can show failures."""
    try:
        conn = connect_db()
        try:
            conn.execute(
                """
                INSERT INTO control_panel_discord_sync
                (visit_id, action, case_id, dm_status, case_log_url, case_log_status, alert_status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (visit_id, action, case_id, dm_status, case_log_url, case_log_status, alert_status, int(time.time())),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("[ControlPanel] Could not record Discord delivery for visit #%s: %s", visit_id, exc)


def _sync_action_to_discord(
    *,
    visit_id=None,
    action: str,
    mod_name: str,
    reason: str,
    case_id: str | None,
    discord_id: str,
    alert_channel,
    alert_message,
    expires_at: int | None,
    warn_count: int,
    web_user_id: str | None = None,
    web_name: str | None = None,
) -> None:
    """Send the flight logger's Discord messages for a web action (runs in a thread).

    For a website account outside the server (web_user_id), there is no DM or mention:
    the case log and the closed alert name the website account instead.
    """
    from utils import flight_discord
    from utils.discord_membership import fetch_guild_member_snapshot

    member_name, has_access = web_name, None
    if web_user_id and not discord_id:
        discord_id = web_user_id
    if discord_id.isdigit():
        try:
            snap = fetch_guild_member_snapshot(discord_id)
            member_name = snap.get("username")
            has_access = str(Config.ISLAND_ACCESS_ROLE) in snap.get("roles", [])
        except Exception:
            pass

    mod_log_url = None
    dm_status = case_log_status = None
    if case_id:
        dm_status = flight_discord.send_member_dm(discord_id, action, reason, case_id) if discord_id.isdigit() else "skipped"
        mod_log_url = flight_discord.post_case_log(
            discord_id, member_name, mod_name, action, reason, case_id,
            warn_count=warn_count, expires_at=expires_at,
        )
        case_log_status = "sent" if mod_log_url else "failed"

    alert_status = "none"
    if alert_channel and alert_message:
        from utils.flight_alert_sync import update_flight_alert
        alert_status = "updated" if update_flight_alert(
            alert_channel, alert_message, action, mod_name,
            target_id=discord_id or None,
            target_name=member_name,
            reason=reason if case_id else None,
            mod_log_url=mod_log_url,
            has_access=has_access,
        ) else "failed"

    if visit_id is not None:
        _record_discord_sync(visit_id, action, case_id, dm_status, mod_log_url, case_log_status, alert_status)


@control_panel_bp.route("/notice/acknowledge", methods=["POST"])
def acknowledge_notice():
    """Acknowledge one or all active warnings for the logged-in user."""
    user = _get_current_user()
    if not user:
        return jsonify({"error": "Authentication required"}), 401

    from utils.unified_auth import resolve_all_user_ids
    cand_ids = resolve_all_user_ids(user)
    if not cand_ids:
        return jsonify({"error": "User identifier not found"}), 400

    data = request.get_json(silent=True) or {}
    warning_id = data.get("warning_id")
    now = int(time.time())

    init_control_panel_db()  # warnings.web_user_id
    conn = connect_db()
    try:
        placeholders = ",".join("?" for _ in cand_ids)
        owner = f"(user_id IN ({placeholders}) OR web_user_id IN ({placeholders}))"
        if warning_id:
            conn.execute(
                f"UPDATE warnings SET acknowledged_at = ? WHERE id = ? AND {owner}",
                [now, warning_id] + cand_ids + cand_ids,
            )
        else:
            conn.execute(
                f"UPDATE warnings SET acknowledged_at = ? WHERE acknowledged_at IS NULL AND {owner}",
                [now] + cand_ids + cand_ids,
            )
        conn.commit()
        return jsonify({"success": True, "acknowledged_at": now})
    except Exception as exc:
        logger.error("[ControlPanel] Error acknowledging notice: %s", exc)
        return jsonify({"error": "Failed to acknowledge notice"}), 500
    finally:
        conn.close()


@control_panel_bp.route("/notice/my-notices", methods=["GET"])
def get_my_notices():
    """Get all moderation notices and active restriction status for caller."""
    user = _get_current_user()
    if not user:
        return jsonify({"error": "Authentication required"}), 401

    notices = get_user_moderation_notices(user)
    return jsonify(notices)


@control_panel_bp.route("/action/undo", methods=["POST"])
@require_mod_auth
def undo_action():
    """Reverse a moderation action: reopen the flight and, for a web warn/timeout/ban,
    give back the island access it removed, tell the member, and post to XLog."""
    data = request.get_json(silent=True) or {}
    visit_id = data.get("visit_id")
    if not visit_id:
        return jsonify({"error": "visit_id is required"}), 400

    user = _get_current_user() or {}
    mod_name = user.get("username") or "Moderator"
    now = int(time.time())

    conn = connect_db()
    try:
        cur = conn.execute(
            "SELECT review_status, handled_by, handled_at, ign, destination, origin_island, "
            "alert_channel_id, alert_message_id FROM island_visits WHERE id = ?",
            (visit_id,),
        )
        row = cur.fetchone()
        if not row:
            return jsonify({"error": "Flight visit not found"}), 404

        prev_status, prev_mod, prev_handled_at, ign, destination, origin, alert_channel, alert_message = row

        # The web punishment being reversed, if any (its audit record holds what was revoked)
        punishment = None
        if prev_status == "actioned" and prev_handled_at:
            prow = conn.execute(
                """
                SELECT action, details FROM dashboard_audit_events
                WHERE target = ? AND created_at = ?
                  AND action IN ('CONTROL_PANEL_WARN', 'CONTROL_PANEL_TIMEOUT', 'CONTROL_PANEL_BAN')
                ORDER BY id DESC LIMIT 1
                """,
                (f"visit:{visit_id}", prev_handled_at),
            ).fetchone()
            if prow:
                try:
                    punishment = {"action": prow[0].replace("CONTROL_PANEL_", "").lower(), **json.loads(prow[1] or "{}")}
                except (TypeError, ValueError):
                    punishment = None
            elif str(prev_mod or "").endswith("(Discord)"):
                # Punished with the Discord buttons: the flight logger recorded what it revoked
                prow = conn.execute(
                    """
                    SELECT details FROM dashboard_audit_events
                    WHERE target = ? AND action = 'DISCORD_PUNISHMENT' AND created_at BETWEEN ? AND ?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (f"visit:{visit_id}", prev_handled_at - 600, prev_handled_at + 60),
                ).fetchone()
                if prow:
                    try:
                        punishment = {**json.loads(prow[0] or "{}"), "source": "discord"}
                    except (TypeError, ValueError):
                        punishment = None
                if punishment:
                    if punishment.get("warning_id"):
                        conn.execute("DELETE FROM warnings WHERE id = ?", (punishment["warning_id"],))
                    if punishment.get("action") == "ban":
                        conn.execute(
                            """
                            UPDATE user_moderation_status SET is_active = 0, reverted_at = ?, reverted_by = ?
                            WHERE is_active = 1 AND action_type = 'BAN' AND (discord_id = ? OR user_id = ?)
                              AND mod_name = ?
                            """,
                            (now, mod_name, punishment.get("target_discord_id"), punishment.get("target_discord_id"),
                             f"Discord:{punishment.get('mod_id')}"),
                        )

        # Reset the visit, plus any re-join attempts resolved by the same action
        conn.execute(
            "UPDATE island_visits SET review_status = 'pending', handled_by = NULL, handled_at = NULL WHERE id = ?",
            (visit_id,),
        )
        if prev_handled_at:
            conn.execute(
                """
                UPDATE island_visits SET review_status = 'pending', handled_by = NULL, handled_at = NULL
                WHERE handled_at = ? AND handled_by = ? AND LOWER(ign) = LOWER(?) AND LOWER(destination) = LOWER(?)
                """,
                (prev_handled_at, prev_mod, ign, destination),
            )

            # Remove the strike and lift the restriction this action created
            # (execute_action stamps them with the same time as handled_at).
            conn.execute(
                "DELETE FROM warnings WHERE visit_id = ? AND timestamp = ? AND action_type IN ('WARN', 'TIMEOUT', 'BAN')",
                (visit_id, prev_handled_at),
            )
            conn.execute(
                """
                UPDATE user_moderation_status SET is_active = 0, reverted_at = ?, reverted_by = ?
                WHERE is_active = 1 AND created_at = ? AND mod_name = ?
                """,
                (now, mod_name, prev_handled_at, prev_mod),
            )

        # Audit log reversal
        try:
            conn.execute(
                """
                INSERT INTO dashboard_audit_events
                (actor_user_id, actor_name, action, target, details, created_at)
                VALUES (?, ?, 'CONTROL_PANEL_ACTION_REVERSED', ?, ?, ?)
                """,
                (
                    user.get("user_id") or "system",
                    mod_name,
                    f"visit:{visit_id}",
                    json.dumps({"reverted_from_status": prev_status, "reverted_mod": prev_mod}),
                    now,
                ),
            )
        except Exception:
            pass

        conn.commit()
    finally:
        conn.close()

    if alert_channel and alert_message:
        try:
            from utils.flight_alert_sync import update_flight_alert_async
            update_flight_alert_async(alert_channel, alert_message, "undo", mod_name)
        except Exception as exc:
            logger.warning("[ControlPanel] Failed to dispatch flight alert reopen: %s", exc)

    message = "Action reversed. Flight is pending again."
    if punishment:
        threading.Thread(
            target=_reverse_punishment_effects,
            args=(punishment, mod_name, visit_id, ign, destination, origin),
            daemon=True,
        ).start()
        message = f"{punishment['action'].title()} reversed: restoring island access and notifying the member."

    return jsonify({
        "success": True,
        "visit_id": visit_id,
        "review_status": "pending",
        "message": message,
    })


def _reverse_punishment_effects(punishment: dict, mod_name: str, visit_id, ign, destination, origin) -> None:
    """Give back the island access a web warn/timeout/ban removed, DM the member, post to XLog."""
    discord_id = str(punishment.get("target_discord_id") or punishment.get("target_user_id") or "").strip()
    restored = False
    if punishment.get("source") == "discord" and punishment.get("action") == "ban" and discord_id.isdigit():
        # A Discord-button ban is a real server ban; lift it so the member can rejoin
        try:
            from utils.discord_http import request as discord_request
            from utils.discord_membership import _bot_headers
            discord_request(
                f"https://discord.com/api/v10/guilds/{Config.GUILD_ID}/bans/{discord_id}",
                method="DELETE", headers=_bot_headers(), timeout=8,
            )
        except Exception as exc:
            logger.warning("[ControlPanel] Could not lift Discord ban for %s: %s", discord_id, exc)
    try:
        from utils.discord_membership import restore_sub_rules_acceptance
        restored = restore_sub_rules_acceptance(discord_id, punishment.get("revoked_access"))
    except Exception as exc:
        logger.warning("[ControlPanel] Could not restore island access for %s: %s", discord_id, exc)

    reason = f"{punishment['action'].upper()} reversed (original reason: {punishment.get('reason') or 'n/a'})"
    try:
        from utils.xlog_sync import post_xlog_event
        post_xlog_event(
            "undo",
            mod_name,
            reason,
            discord_id=discord_id or None,
            ign=ign,
            destination=destination,
            origin=origin,
            visit_id=visit_id,
            role_restored=restored,
        )
    except Exception as exc:
        logger.warning("[ControlPanel] Failed to post undo to XLog: %s", exc)

    if discord_id.isdigit():
        try:
            from utils.flight_discord import make_case_id, send_member_dm
            case_id = punishment.get("case_id") or make_case_id()
            dm_status = send_member_dm(discord_id, "undo", reason, case_id)
            _record_discord_sync(visit_id, "undo", case_id, dm_status, None, None, None)
        except Exception as exc:
            logger.warning("[ControlPanel] Failed to DM undo to %s: %s", discord_id, exc)


@control_panel_bp.route("/bulk-authorise", methods=["POST"])
@require_mod_auth
def bulk_authorise():
    """Batch approve all currently pending sub-island flights with match score >= 90."""
    user = _get_current_user() or {}
    mod_name = user.get("username") or "Moderator"
    now = int(time.time())
    sub_sql, sub_params = sub_destination_filter("destination")

    conn = connect_db()
    try:
        cur = conn.execute(
            f"""
            UPDATE island_visits
            SET review_status = 'authorised', authorized = 1, handled_by = ?, handled_at = ?
            WHERE review_status = 'pending' AND match_score >= 90 AND {sub_sql}
            """,
            (f"{mod_name} (Bulk)", now, *sub_params),
        )
        affected = cur.rowcount
        conn.commit()

        return jsonify({
            "success": True,
            "authorised_count": affected,
            "message": f"Successfully batch-authorised {affected} verified flights.",
        })
    finally:
        conn.close()


@control_panel_bp.route("/history", methods=["GET"])
@require_mod_auth
def get_action_history():
    """Return latest moderation actions audit log."""
    limit = min(int(request.args.get("limit", 50)), 100)
    conn = connect_db()
    try:
        cur = conn.execute(
            """
            SELECT id, actor_name, action, target, details, created_at
            FROM dashboard_audit_events
            WHERE action LIKE 'CONTROL_PANEL_%'
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = cur.fetchall()

        events = []
        for r in rows:
            try:
                details = json.loads(r[4] or "{}")
            except Exception:
                details = {}
            events.append({
                "id": r[0],
                "actor_name": r[1],
                "action": r[2],
                "target": r[3],
                "details": details,
                "created_at": r[5],
            })

        return jsonify({"history": events})
    finally:
        conn.close()


@control_panel_bp.route("/user-inspect", methods=["GET"])
@require_mod_auth
def user_inspect():
    """Deep inspect a user profile by user_id or character IGN."""
    user_id = request.args.get("user_id", "").strip()
    ign = request.args.get("ign", "").strip()

    if not user_id and not ign:
        return jsonify({"error": "user_id or ign query parameter is required"}), 400

    conn = connect_db()
    try:
        # 1. Saved characters (supporting all linked aliases)
        chars = []
        if user_id:
            from utils.unified_auth import resolve_all_user_ids
            cand_uids = resolve_all_user_ids(user_id)
            if not cand_uids:
                cand_uids = [str(user_id)]
            placeholders = ",".join("?" for _ in cand_uids)
            cur = conn.execute(
                f"""
                SELECT id, ign, island_name, icon, is_default, created_at 
                FROM user_saved_characters 
                WHERE user_id IN ({placeholders})
                ORDER BY is_default DESC, created_at DESC
                """,
                cand_uids,
            )
            for r in cur.fetchall():
                chars.append({
                    "id": r[0],
                    "ign": r[1],
                    "island_name": r[2],
                    "icon": r[3],
                    "is_default": bool(r[4]),
                    "created_at": r[5],
                })
        elif ign:
            cur = conn.execute(
                """
                SELECT id, ign, island_name, icon, is_default, created_at 
                FROM user_saved_characters 
                WHERE LOWER(ign) = LOWER(?)
                ORDER BY is_default DESC, created_at DESC
                """,
                (ign,),
            )
            for r in cur.fetchall():
                chars.append({
                    "id": r[0],
                    "ign": r[1],
                    "island_name": r[2],
                    "icon": r[3],
                    "is_default": bool(r[4]),
                    "created_at": r[5],
                })

        # 2. Warnings history
        numeric_uid = int(user_id) if str(user_id).isdigit() else 0
        # Website accounts outside Discord: warnings carry web_user_id, flights carry the
        # account in their verdict (island_visits.user_id is a numeric Discord ID).
        web_uid = user_id if user_id and not numeric_uid else ""
        cur = conn.execute(
            """
            SELECT w.id, w.reason, w.mod_id, w.timestamp, w.action_type, w.mod_name
            FROM warnings w
            LEFT JOIN island_visits iv ON w.visit_id = iv.id
            WHERE (w.user_id = ? AND ? > 0) OR (? <> '' AND w.web_user_id = ?) OR (? <> '' AND LOWER(iv.ign) = LOWER(?))
            ORDER BY w.id DESC LIMIT 20
            """,
            (numeric_uid, numeric_uid, web_uid, web_uid, ign, ign),
        )
        warnings_list = [
            {"id": r[0], "reason": r[1], "mod_id": r[2], "timestamp": r[3], "action_type": r[4], "mod_name": r[5]}
            for r in cur.fetchall()
        ]

        # 3. Recent flights
        cur = conn.execute(
            """
            SELECT id, ign, origin_island, destination, review_status, timestamp
            FROM island_visits
            WHERE ((? > 0 AND user_id = ?) OR (? <> '' AND logger_detail LIKE ?) OR LOWER(ign) = LOWER(?))
            ORDER BY id DESC LIMIT 15
            """,
            (numeric_uid, numeric_uid, web_uid, f'%"id": "{web_uid}"%', ign or ""),
        )
        recent_flights = [
            {"id": r[0], "ign": r[1], "origin_island": r[2], "destination": r[3], "review_status": r[4], "timestamp": r[5]}
            for r in cur.fetchall()
        ]

        # 4. Active restrictions
        active_restriction = check_user_active_restriction(user_id)

        # 5. Live Discord membership (what the flight logger checks via member roles)
        discord_member = None
        if numeric_uid:
            from utils.discord_membership import (
                DiscordNotGuildMember,
                fetch_guild_member_snapshot,
            )
            try:
                snap = fetch_guild_member_snapshot(str(numeric_uid))
                discord_member = {
                    "in_guild": True,
                    "display_name": snap.get("username"),
                    "account_name": snap.get("account_name"),
                    "avatar": snap.get("avatar"),
                    "joined_at": snap.get("joined_at"),
                    "has_island_access": str(Config.ISLAND_ACCESS_ROLE) in snap.get("roles", []),
                }
            except DiscordNotGuildMember:
                discord_member = {"in_guild": False}
            except Exception as exc:
                logger.debug("[ControlPanel] Discord lookup failed for %s: %s", numeric_uid, exc)

        # 6. Website account for members outside Discord (what the logger checks instead of roles)
        website_account = None
        if web_uid:
            from utils.website_identity import account_tiers, is_website_only, load_website_account
            account = load_website_account(web_uid)
            if account:
                website_account = {
                    "name": account_label(account),
                    "providers": account.get("connected_providers") or [],
                    "tiers": account_tiers(account),
                    "website_only": is_website_only(account),
                }

        return jsonify({
            "user_id": user_id,
            "ign": ign,
            "characters": chars,
            "warnings": warnings_list,
            "recent_flights": recent_flights,
            "active_restriction": active_restriction,
            "discord_member": discord_member,
            "website_account": website_account,
        })
    finally:
        conn.close()


# -----------------------------------------------------------------------------
# XLog: sanctions, pardon, and unified moderation log
# -----------------------------------------------------------------------------

@control_panel_bp.route("/sanctions", methods=["GET"])
@require_mod_auth
def list_active_sanctions():
    """List active bans and unexpired timeouts."""
    init_control_panel_db()
    now = int(time.time())
    conn = connect_db()
    try:
        cur = conn.execute(
            """
            SELECT id, user_id, discord_id, action_type, reason, mod_id, mod_name, created_at, expires_at
            FROM user_moderation_status
            WHERE is_active = 1 AND (expires_at IS NULL OR expires_at > ?)
            ORDER BY created_at DESC
            LIMIT 200
            """,
            (now,),
        )
        sanctions = [
            {
                "id": r[0],
                "user_id": r[1],
                "discord_id": r[2],
                "action_type": r[3],
                "reason": r[4],
                "mod_id": r[5],
                "mod_name": r[6] or r[5],
                "created_at": r[7],
                "expires_at": r[8],
            }
            for r in cur.fetchall()
        ]
        return jsonify({"sanctions": sanctions, "count": len(sanctions)})
    finally:
        conn.close()


@control_panel_bp.route("/pardon", methods=["POST"])
@require_mod_auth
def pardon_sanction():
    """Deactivate an active ban/timeout and mirror the pardon to Discord XLog."""
    init_control_panel_db()
    data = request.get_json(silent=True) or {}
    sanction_id = data.get("sanction_id")
    reason = str(data.get("reason") or "").strip() or "Pardoned by moderator"
    if not sanction_id:
        return jsonify({"error": "sanction_id is required"}), 400

    user = _get_current_user() or {}
    mod_name = user.get("username") or user.get("user_id") or "Moderator"
    mod_id = str(user.get("user_id") or "system")
    now = int(time.time())

    conn = connect_db()
    try:
        row = conn.execute(
            "SELECT user_id, discord_id, action_type, is_active FROM user_moderation_status WHERE id = ?",
            (sanction_id,),
        ).fetchone()
        if not row:
            return jsonify({"error": "Sanction not found"}), 404
        target_user, target_discord, action_type, is_active = row
        if not is_active:
            return jsonify({"error": "Sanction is already inactive"}), 409
        # Moderators can ban, so they can also lift bans (require_mod_auth checked the role)

        conn.execute(
            "UPDATE user_moderation_status SET is_active = 0, reverted_at = ?, reverted_by = ? WHERE id = ?",
            (now, mod_name, sanction_id),
        )
        try:
            conn.execute(
                """
                INSERT INTO dashboard_audit_events
                (actor_user_id, actor_name, action, target, details, created_at)
                VALUES (?, ?, 'CONTROL_PANEL_PARDON', ?, ?, ?)
                """,
                (
                    mod_id,
                    mod_name,
                    f"user:{target_user}",
                    json.dumps({"sanction_id": sanction_id, "action_type": action_type, "reason": reason}),
                    now,
                ),
            )
        except Exception:
            pass
        conn.commit()
    finally:
        conn.close()

    try:
        from utils.xlog_sync import post_xlog_event_async
        post_xlog_event_async(
            "pardon",
            mod_name,
            f"[{str(action_type).upper()}] {reason}",
            discord_id=target_discord,
            user_id=target_user,
            source="Web Dashboard",
        )
    except Exception as exc:
        logger.warning("[ControlPanel] Failed to dispatch pardon XLog sync: %s", exc)

    return jsonify({"success": True, "sanction_id": sanction_id, "message": "Sanction pardoned."})


@control_panel_bp.route("/xlog", methods=["GET"])
@require_mod_auth
def xlog_feed():
    """Unified moderation log (web + Discord actions share the warnings table)."""
    limit = max(1, min(int(request.args.get("limit", 50) or 50), 200))
    offset = max(0, int(request.args.get("offset", 0) or 0))
    action_type = request.args.get("action_type", "").strip().upper()
    q = request.args.get("q", "").strip()

    where, params = [], []
    if action_type:
        where.append("UPPER(w.action_type) = ?")
        params.append(action_type)
    if q:
        where.append("(CAST(w.user_id AS CHAR) LIKE ? OR w.reason LIKE ? OR iv.ign LIKE ? OR w.mod_name LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like, like, like])
    clause = f"WHERE {' AND '.join(where)}" if where else ""

    conn = connect_db()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) FROM warnings w LEFT JOIN island_visits iv ON w.visit_id = iv.id {clause}",
            params,
        ).fetchone()[0]
        cur = conn.execute(
            f"""
            SELECT w.id, w.user_id, w.reason, w.mod_id, w.mod_name, w.timestamp, w.action_type,
                   w.visit_id, iv.ign, iv.destination
            FROM warnings w LEFT JOIN island_visits iv ON w.visit_id = iv.id
            {clause}
            ORDER BY w.timestamp DESC, w.id DESC
            LIMIT ? OFFSET ?
            """,
            params + [limit, offset],
        )
        entries = [
            {
                "id": r[0],
                "user_id": r[1],
                "reason": r[2],
                "mod_id": r[3],
                "mod_name": r[4],
                "timestamp": r[5],
                "action_type": (r[6] or "WARN").upper(),
                "visit_id": r[7],
                "ign": r[8],
                "destination": r[9],
            }
            for r in cur.fetchall()
        ]
        return jsonify({"entries": entries, "total": total, "limit": limit, "offset": offset})
    finally:
        conn.close()
