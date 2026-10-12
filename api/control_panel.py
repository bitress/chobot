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
import time
import unicodedata
from functools import wraps
from typing import Any, Dict, List, Optional, Tuple

from flask import Blueprint, jsonify, request, session
from thefuzz import fuzz

from utils.auth_tokens import get_auth_user
from utils.config import Config
from utils.database import connect_db, get_backend
from utils.helpers import clean_text

logger = logging.getLogger("ControlPanel")

control_panel_bp = Blueprint("control_panel", __name__, url_prefix="/api/control-panel")

# -----------------------------------------------------------------------------
# Database Schema Initialization & Migrations
# -----------------------------------------------------------------------------

def init_control_panel_db() -> None:
    """Safely apply migrations for Control Panel tables and columns across SQLite and MySQL."""
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
            ]
            for col_name, col_def in new_cols:
                if col_name.lower() not in existing_cols:
                    try:
                        conn.execute(f"ALTER TABLE island_visits ADD COLUMN {col_name} {col_def}")
                        conn.commit()
                    except Exception:
                        pass  # Column already exists or table not ready

            try:
                conn.execute("CREATE INDEX ix_island_visits_review_status ON island_visits(review_status)")
                conn.commit()
            except Exception:
                pass

            # Backfill existing visits if review_status is empty
            try:
                conn.execute(
                    "UPDATE island_visits SET review_status = 'authorised' "
                    "WHERE authorized = 1 AND (review_status IS NULL OR review_status = 'pending')"
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

            # 3. Idempotency store
            conn.execute("""
            CREATE TABLE IF NOT EXISTS control_panel_idempotency (
                `key` VARCHAR(255) PRIMARY KEY,
                response_json TEXT NOT NULL,
                created_at BIGINT NOT NULL
            )
            """)
            conn.commit()

            logger.info("[ControlPanel] Database migrations verified.")
        finally:
            conn.close()
    except Exception as exc:
        logger.error("[ControlPanel] DB initialization failed: %s", exc, exc_info=True)


# -----------------------------------------------------------------------------
# Active Restriction Checker (Used by Dodo Reveal Gate)
# -----------------------------------------------------------------------------

def check_user_active_restriction(user_id: str | None, discord_id: str | None = None) -> dict | None:
    """Check if user has an active ban or unexpired timeout.
    Returns restriction dict or None.
    """
    if not user_id and not discord_id:
        return None

    now = int(time.time())
    try:
        conn = connect_db()
        try:
            cur = conn.execute(
                """
                SELECT id, user_id, discord_id, action_type, reason, created_at, expires_at
                FROM user_moderation_status
                WHERE is_active = 1
                  AND (user_id = ? OR (discord_id IS NOT NULL AND discord_id = ?))
                  AND (expires_at IS NULL OR expires_at > ?)
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (str(user_id or ""), str(discord_id or ""), now),
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
                }
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("[ControlPanel] Restriction check failed: %s", exc)
    return None


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


def compute_identity_match_score(
    flight_ign: str,
    flight_origin: str,
    target_ign: str,
    target_island: str,
) -> int:
    """Calculate 0-100 match score using Levenshtein fuzzy comparison."""
    f_ign = normalize_text_for_comparison(flight_ign)
    f_isl = normalize_text_for_comparison(flight_origin)
    t_ign = normalize_text_for_comparison(target_ign)
    t_isl = normalize_text_for_comparison(target_island)

    if not f_ign or not t_ign:
        return 0

    if f_ign == t_ign and f_isl == t_isl:
        return 100

    ign_ratio = fuzz.ratio(f_ign, t_ign)
    isl_ratio = fuzz.ratio(f_isl, t_isl) if f_isl and t_isl else 50

    # Weight character name 70%, origin island 30%
    weighted = int((ign_ratio * 0.70) + (isl_ratio * 0.30))

    # Exact IGN match with missing or slight island difference gives at least 90
    if f_ign == t_ign and isl_ratio >= 70:
        return max(weighted, 90)

    return min(100, max(0, weighted))


def correlate_single_flight(
    flight: dict,
    reveals_for_dest: list[dict],
    saved_chars_by_user: dict[str, list[dict]],
    all_saved_chars: list[dict],
) -> dict:
    """Correlate a flight against candidate Dodo reveals and saved character slots."""
    f_ign = flight["ign"]
    f_isl = flight["origin_island"]
    f_ts = flight["timestamp"]

    best_candidate: dict | None = None
    best_score = 0
    runner_ups: list[dict] = []

    # 1. Search against recent Dodo reveals for this destination island
    for rev in reveals_for_dest:
        rev_user_id = rev["user_id"]
        rev_created_at = rev["created_at"]
        time_diff = abs(f_ts - rev_created_at)

        # Look up saved character slots for this revealing user
        user_chars = saved_chars_by_user.get(rev_user_id, [])

        max_char_score = 0
        best_char = None
        for char in user_chars:
            score = compute_identity_match_score(f_ign, f_isl, char["ign"], char["island_name"])
            if score > max_char_score:
                max_char_score = score
                best_char = char

        # Also check against Discord nickname if formatted as "IGN | Island"
        nick_score = 0
        if rev.get("nickname"):
            parts = [p.strip() for p in rev["nickname"].replace("/", "|").split("|")]
            if len(parts) >= 2:
                nick_score = compute_identity_match_score(f_ign, f_isl, parts[0], parts[1])
            elif len(parts) == 1:
                nick_score = fuzz.ratio(normalize_text_for_comparison(f_ign), normalize_text_for_comparison(parts[0]))

        effective_score = max(max_char_score, nick_score)

        candidate_record = {
            "reveal_id": rev["id"],
            "user_id": rev_user_id,
            "username": rev.get("username", ""),
            "nickname": rev.get("nickname", ""),
            "reveal_created_at": rev_created_at,
            "time_diff_seconds": time_diff,
            "matched_character": best_char,
            "score": effective_score,
            "all_user_characters": user_chars,
        }

        if effective_score > best_score:
            if best_candidate:
                runner_ups.append(best_candidate)
            best_score = effective_score
            best_candidate = candidate_record
        elif effective_score >= 60:
            runner_ups.append(candidate_record)

    # 2. Fallback: If no Dodo reveal matched with score >= 70, check globally across all saved characters
    if best_score < 70 and all_saved_chars:
        for char in all_saved_chars:
            g_score = compute_identity_match_score(f_ign, f_isl, char["ign"], char["island_name"])
            if g_score > best_score:
                best_score = g_score
                best_candidate = {
                    "reveal_id": None,
                    "user_id": char["user_id"],
                    "username": "Registered Member",
                    "nickname": "",
                    "reveal_created_at": None,
                    "time_diff_seconds": None,
                    "matched_character": char,
                    "score": g_score,
                    "all_user_characters": [char],
                }

    # Classify match status
    if best_score >= 90:
        match_status = "match"
    elif best_score >= 70:
        match_status = "partial"
    else:
        match_status = "mismatch"

    return {
        "match_status": match_status,
        "match_score": best_score,
        "primary_candidate": best_candidate,
        "runner_ups": sorted(runner_ups, key=lambda r: r["score"], reverse=True)[:3],
    }


# -----------------------------------------------------------------------------
# Endpoints
# -----------------------------------------------------------------------------

@control_panel_bp.route("/live-feed", methods=["GET"])
@require_mod_auth
def get_live_feed():
    """Fetch live feed of Sub-Island flights correlated with Dodo reveals and saved characters."""
    init_control_panel_db()

    limit = min(int(request.args.get("limit", 50)), 100)
    since_id = request.args.get("since_id")
    island_filter = request.args.get("island")
    status_filter = request.args.get("status", "all").lower().strip()
    search_query = request.args.get("search", "").strip().lower()

    conn = connect_db()
    try:
        # Build flights query
        where_clauses = ["1=1"]
        params: list[Any] = []

        if since_id and since_id.isdigit():
            where_clauses.append("v.id > ?")
            params.append(int(since_id))

        if island_filter:
            where_clauses.append("LOWER(v.destination) = LOWER(?)")
            params.append(island_filter.strip())

        if status_filter == "pending":
            where_clauses.append("v.review_status = 'pending'")
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
                   v.handled_by, v.handled_at, v.match_score, v.correlated_reveal_id
            FROM island_visits v
            WHERE {where_sql}
            ORDER BY v.id DESC
            LIMIT ?
            """,
            tuple(params + [limit]),
        )
        flight_rows = cur.fetchall()

        if not flight_rows:
            return jsonify({"flights": [], "has_more": False, "server_time": int(time.time())})

        # Find min/max timestamps to bound Dodo reveals query efficiently
        timestamps = [row[7] for row in flight_rows]
        min_ts = min(timestamps) - 2400  # 40 min before earliest flight
        max_ts = max(timestamps) + 300

        # Fetch recent Dodo reveals in the window
        cur = conn.execute(
            """
            SELECT id, user_id, island_clean, channel_id, message_url, username, nickname, created_at
            FROM dodo_reveal_messages
            WHERE created_at BETWEEN ? AND ?
            ORDER BY created_at DESC
            """,
            (min_ts, max_ts),
        )
        reveal_rows = cur.fetchall()

        reveals_by_island: dict[str, list[dict]] = {}
        for r in reveal_rows:
            rev_dict = {
                "id": r[0],
                "user_id": r[1],
                "island_clean": r[2],
                "channel_id": r[3],
                "message_url": r[4],
                "username": r[5],
                "nickname": r[6],
                "created_at": r[7],
            }
            isl_key = clean_text(r[2]).lower()
            reveals_by_island.setdefault(isl_key, []).append(rev_dict)

        # Fetch saved characters for revealers and global lookup
        cur = conn.execute("SELECT user_id, id, ign, island_name, icon, is_default FROM user_saved_characters")
        all_chars_rows = cur.fetchall()

        all_saved_chars: list[dict] = []
        saved_chars_by_user: dict[str, list[dict]] = {}
        for c in all_chars_rows:
            c_dict = {
                "user_id": c[0],
                "id": c[1],
                "ign": c[2],
                "island_name": c[3],
                "icon": c[4],
                "is_default": bool(c[5]),
            }
            all_saved_chars.append(c_dict)
            saved_chars_by_user.setdefault(c[0], []).append(c_dict)

        # Fetch strike counts (active warnings count)
        cur = conn.execute(
            """
            SELECT user_id, COUNT(*) 
            FROM warnings 
            WHERE action_type IN ('WARN', 'TIMEOUT', 'BAN')
            GROUP BY user_id
            """
        )
        strikes_map = {str(row[0]): row[1] for row in cur.fetchall()}

        # Correlate each flight
        feed_items = []
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
            }

            dest_key = clean_text(flight["destination"]).lower()
            relevant_reveals = reveals_by_island.get(dest_key, [])

            # Run correlation
            correlation = correlate_single_flight(
                flight, relevant_reveals, saved_chars_by_user, all_saved_chars
            )

            primary = correlation["primary_candidate"]
            revealer_id = primary["user_id"] if primary else (str(flight["user_id"]) if flight["user_id"] else "")
            strike_count = strikes_map.get(revealer_id, 0)

            # Smart next-step recommendation
            if strike_count == 0:
                suggestion = "Clean record • Recommend: Authorise or Warn"
            elif strike_count == 1:
                suggestion = "1 prior warning • Recommend: 24h Timeout"
            elif strike_count >= 2:
                suggestion = f"{strike_count} prior strikes • Recommend: 7d Timeout or Ban"
            else:
                suggestion = "Review identity match"

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
                "match_status": correlation["match_status"],
                "match_score": correlation["match_score"],
                "strike_count": strike_count,
                "suggestion": suggestion,
                "primary_candidate": primary,
                "runner_ups": correlation["runner_ups"],
            }

            # Optional filter by alerts only
            if status_filter == "alerts" and correlation["match_status"] == "match":
                continue

            # Optional search filtering
            if search_query:
                combined_text = (
                    f"{flight['ign']} {flight['origin_island']} {flight['destination']} "
                    f"{primary.get('username', '') if primary else ''} "
                    f"{primary.get('nickname', '') if primary else ''}"
                ).lower()
                if search_query not in combined_text:
                    continue

            feed_items.append(item)

        return jsonify({
            "flights": feed_items,
            "count": len(feed_items),
            "server_time": int(time.time()),
        })
    finally:
        conn.close()


@control_panel_bp.route("/stats", methods=["GET"])
@require_mod_auth
def get_stats():
    """Return live KPI stats for the Control Panel top bar."""
    now = int(time.time())
    one_day_ago = now - 86400

    conn = connect_db()
    try:
        # Flights today
        cur = conn.execute("SELECT COUNT(*) FROM island_visits WHERE timestamp >= ?", (one_day_ago,))
        flights_today = cur.fetchone()[0]

        # Pending count
        cur = conn.execute("SELECT COUNT(*) FROM island_visits WHERE review_status = 'pending'")
        pending_total = cur.fetchone()[0]

        # Authorised today
        cur = conn.execute(
            "SELECT COUNT(*) FROM island_visits WHERE review_status = 'authorised' AND handled_at >= ?",
            (one_day_ago,),
        )
        authorised_today = cur.fetchone()[0]

        # Actioned today
        cur = conn.execute(
            "SELECT COUNT(*) FROM island_visits WHERE review_status = 'actioned' AND handled_at >= ?",
            (one_day_ago,),
        )
        actioned_today = cur.fetchone()[0]

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

        valid_actions = {"authorise", "dismiss", "warn", "timeout", "ban"}
        if action not in valid_actions:
            return jsonify({"error": f"Invalid action: {action}. Must be one of {valid_actions}"}), 400

        user = _get_current_user() or {}
        mod_name = user.get("username") or user.get("user_id") or "Moderator"
        mod_id = str(user.get("user_id") or "system")
        warn_user_id = int(discord_id) if str(discord_id).isdigit() else (int(user_id) if str(user_id).isdigit() else 0)
        warn_mod_id = int(user.get("discord_id")) if str(user.get("discord_id") or "").isdigit() else (int(mod_id) if str(mod_id).isdigit() else 0)
        now = int(time.time())

        # Check optimistic concurrency
        cur = conn.execute("SELECT review_status, handled_by, destination, ign, origin_island FROM island_visits WHERE id = ?", (visit_id,))
        visit_row = cur.fetchone()
        if not visit_row:
            return jsonify({"error": "Flight record not found"}), 404

        current_status, handled_by, dest_island, flight_ign, flight_origin = visit_row
        if current_status != "pending" and not force:
            return jsonify({
                "error": f"Flight was already handled by {handled_by or 'another moderator'} ({current_status}).",
                "code": "already_handled",
                "handled_by": handled_by,
                "current_status": current_status,
            }), 409

        # Handle specific actions
        status_to_set = "actioned"
        expires_at = None

        if action == "authorise":
            status_to_set = "authorised"
            conn.execute(
                "UPDATE island_visits SET review_status = 'authorised', authorized = 1, handled_by = ?, handled_at = ? WHERE id = ?",
                (mod_name, now, visit_id),
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
            conn.execute(
                """
                INSERT INTO warnings (user_id, guild_id, reason, mod_id, timestamp, visit_id, action_type)
                VALUES (?, ?, ?, ?, ?, ?, 'WARN')
                """,
                (warn_user_id, Config.GUILD_ID or 0, reason, warn_mod_id, now, visit_id),
            )
        elif action == "timeout":
            status_to_set = "actioned"
            duration_hours = max(1, duration_hours or 24)
            expires_at = now + (duration_hours * 3600)

            conn.execute(
                "UPDATE island_visits SET review_status = 'actioned', handled_by = ?, handled_at = ? WHERE id = ?",
                (mod_name, now, visit_id),
            )
            conn.execute(
                """
                INSERT INTO warnings (user_id, guild_id, reason, mod_id, timestamp, visit_id, action_type)
                VALUES (?, ?, ?, ?, ?, ?, 'TIMEOUT')
                """,
                (warn_user_id, Config.GUILD_ID or 0, reason, warn_mod_id, now, visit_id),
            )
            conn.execute(
                """
                INSERT INTO user_moderation_status
                (user_id, discord_id, action_type, reason, mod_id, created_at, expires_at, is_active)
                VALUES (?, ?, 'TIMEOUT', ?, ?, ?, ?, 1)
                """,
                (user_id or discord_id or flight_ign, discord_id or None, reason, mod_name, now, expires_at),
            )
        elif action == "ban":
            # Admin check for permanent ban
            if not _is_admin_user(user):
                return jsonify({"error": "Admin role required to execute permanent bans"}), 403

            status_to_set = "actioned"
            conn.execute(
                "UPDATE island_visits SET review_status = 'actioned', authorized = 0, has_island_access = 0, handled_by = ?, handled_at = ? WHERE id = ?",
                (mod_name, now, visit_id),
            )
            conn.execute(
                """
                INSERT INTO warnings (user_id, guild_id, reason, mod_id, timestamp, visit_id, action_type)
                VALUES (?, ?, ?, ?, ?, ?, 'BAN')
                """,
                (warn_user_id, Config.GUILD_ID or 0, reason, warn_mod_id, now, visit_id),
            )
            conn.execute(
                """
                INSERT INTO user_moderation_status
                (user_id, discord_id, action_type, reason, mod_id, created_at, expires_at, is_active)
                VALUES (?, ?, 'BAN', ?, ?, ?, NULL, 1)
                """,
                (user_id or discord_id or flight_ign, discord_id or None, reason, mod_name, now),
            )

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

        conn.commit()

        response_payload = {
            "success": True,
            "action": action,
            "visit_id": visit_id,
            "review_status": status_to_set,
            "handled_by": mod_name,
            "handled_at": now,
            "expires_at": expires_at,
            "message": f"Action '{action}' applied successfully.",
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


@control_panel_bp.route("/action/undo", methods=["POST"])
@require_mod_auth
def undo_action():
    """Reverse a recently executed moderation action."""
    data = request.get_json(silent=True) or {}
    visit_id = data.get("visit_id")
    if not visit_id:
        return jsonify({"error": "visit_id is required"}), 400

    user = _get_current_user() or {}
    mod_name = user.get("username") or "Moderator"
    now = int(time.time())

    conn = connect_db()
    try:
        cur = conn.execute("SELECT review_status, handled_by, ign FROM island_visits WHERE id = ?", (visit_id,))
        row = cur.fetchone()
        if not row:
            return jsonify({"error": "Flight visit not found"}), 404

        prev_status, prev_mod, ign = row

        # Reset visit status to pending
        conn.execute(
            "UPDATE island_visits SET review_status = 'pending', handled_by = NULL, handled_at = NULL WHERE id = ?",
            (visit_id,),
        )

        # Deactivate any moderation status tied to this visit
        conn.execute(
            "UPDATE user_moderation_status SET is_active = 0, reverted_at = ?, reverted_by = ? WHERE is_active = 1 AND reason LIKE ?",
            (now, mod_name, f"%{ign}%"),
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
        return jsonify({
            "success": True,
            "visit_id": visit_id,
            "review_status": "pending",
            "message": "Action successfully reversed. Flight is pending again.",
        })
    finally:
        conn.close()


@control_panel_bp.route("/bulk-authorise", methods=["POST"])
@require_mod_auth
def bulk_authorise():
    """Batch approve all currently pending flights with match score >= 90."""
    user = _get_current_user() or {}
    mod_name = user.get("username") or "Moderator"
    now = int(time.time())

    conn = connect_db()
    try:
        cur = conn.execute(
            """
            UPDATE island_visits
            SET review_status = 'authorised', authorized = 1, handled_by = ?, handled_at = ?
            WHERE review_status = 'pending' AND match_score >= 90
            """,
            (f"{mod_name} (Bulk)", now),
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
        # 1. Saved characters
        chars = []
        if user_id:
            cur = conn.execute("SELECT id, ign, island_name, icon, is_default, created_at FROM user_saved_characters WHERE user_id = ?", (user_id,))
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
        cur = conn.execute(
            """
            SELECT id, reason, mod_id, timestamp, action_type 
            FROM warnings 
            WHERE (user_id = ? AND ? > 0) OR reason LIKE ?
            ORDER BY id DESC LIMIT 20
            """,
            (numeric_uid, numeric_uid, f"%{ign}%" if ign else "%"),
        )
        warnings_list = [
            {"id": r[0], "reason": r[1], "mod_id": r[2], "timestamp": r[3], "action_type": r[4]}
            for r in cur.fetchall()
        ]

        # 3. Recent flights
        cur = conn.execute(
            """
            SELECT id, ign, origin_island, destination, review_status, timestamp 
            FROM island_visits 
            WHERE ((? > 0 AND user_id = ?) OR LOWER(ign) = LOWER(?))
            ORDER BY id DESC LIMIT 15
            """,
            (numeric_uid, numeric_uid, ign or ""),
        )
        recent_flights = [
            {"id": r[0], "ign": r[1], "origin_island": r[2], "destination": r[3], "review_status": r[4], "timestamp": r[5]}
            for r in cur.fetchall()
        ]

        # 4. Active restrictions
        active_restriction = check_user_active_restriction(user_id)

        return jsonify({
            "user_id": user_id,
            "ign": ign,
            "characters": chars,
            "warnings": warnings_list,
            "recent_flights": recent_flights,
            "active_restriction": active_restriction,
        })
    finally:
        conn.close()
