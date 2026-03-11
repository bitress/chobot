"""
ChoBot Web Dashboard
Mod-only web interface for island management, xlog reports, and analytics.
Access is protected by a secret key (DASHBOARD_SECRET env var).
"""

import os
import sqlite3
import logging
from datetime import datetime, timezone
from functools import wraps

from flask import (
    Blueprint, render_template, request, redirect,
    url_for, session, flash, jsonify,
)

from utils.config import Config

logger = logging.getLogger("Dashboard")

# ---------------------------------------------------------------------------
# Blueprint setup
# ---------------------------------------------------------------------------
dashboard = Blueprint(
    "dashboard",
    __name__,
    template_folder="templates",
    static_folder="static",
    static_url_path="/dashboard/static",
)

# Absolute path to the shared SQLite database
_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "chobot.db",
)

ALLOWED_CATEGORIES = ("public", "member")
ALLOWED_THEMES = ("pink", "teal", "purple", "gold")
ALLOWED_STATUSES = ("ONLINE", "SUB ONLY", "REFRESHING", "OFFLINE")


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
def get_db():
    """Return a synchronous SQLite connection to chobot.db."""
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_dashboard_db():
    """Create dashboard-specific tables if they do not already exist."""
    try:
        conn = get_db()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS island_metadata (
                name       TEXT PRIMARY KEY,
                category   TEXT NOT NULL DEFAULT 'public',
                theme      TEXT NOT NULL DEFAULT 'teal',
                notes      TEXT NOT NULL DEFAULT '',
                updated_at TEXT
            )
            """
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.warning("Could not initialise dashboard DB: %s", exc)


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
def _check_session():
    return bool(session.get("mod_logged_in"))


def _check_bearer():
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer ") and Config.DASHBOARD_SECRET:
        return auth[len("Bearer "):] == Config.DASHBOARD_SECRET
    return False


def login_required(f):
    """Decorator for web routes — redirects to /dashboard/login if not authenticated."""
    @wraps(f)
    def _decorated(*args, **kwargs):
        if not _check_session():
            return redirect(url_for("dashboard.login"))
        return f(*args, **kwargs)
    return _decorated


def api_auth_required(f):
    """Decorator for JSON API routes — returns 401 when token/session is missing."""
    @wraps(f)
    def _decorated(*args, **kwargs):
        if not _check_bearer() and not _check_session():
            return jsonify({"error": "Unauthorized — send 'Authorization: Bearer <DASHBOARD_SECRET>'"}), 401
        return f(*args, **kwargs)
    return _decorated


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------
def _read_file(folder_path, filename):
    try:
        with open(os.path.join(folder_path, filename), "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except Exception:
        return None


def _write_file(folder_path, filename, content):
    with open(os.path.join(folder_path, filename), "w", encoding="utf-8") as fh:
        fh.write(content)


def _collect_islands():
    """Return a sorted list of island dicts from the filesystem."""
    islands = []

    def _scan(directory, island_type):
        if not directory or not os.path.exists(directory):
            return
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.is_dir():
                    islands.append({
                        "name": entry.name.upper(),
                        "path": entry.path,
                        "type": island_type,
                        "dodo": _read_file(entry.path, "Dodo.txt") or "-----",
                        "visitors": _read_file(entry.path, "Visitors.txt") or "0",
                    })

    _scan(Config.DIR_FREE, "Free")
    _scan(Config.DIR_VIP, "VIP")
    islands.sort(key=lambda x: x["name"])
    return islands


def _where_clause(conditions: list) -> str:
    """Build a safe WHERE clause from a list of predefined SQL fragment strings.

    Only hardcoded SQL condition strings (containing '?' placeholders) may be
    passed here — never raw user input.  User-supplied values must be passed
    separately as a params list to the db.execute() call.
    """
    return ("WHERE " + " AND ".join(conditions)) if conditions else ""
    """Convert a Unix timestamp int to a human-readable UTC string."""
    if ts is None:
        return "—"
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return str(ts)


# ===========================================================================
# WEB ROUTES
# ===========================================================================

@dashboard.route("/login", methods=["GET", "POST"])
def login():
    if _check_session():
        return redirect(url_for("dashboard.index"))
    if request.method == "POST":
        secret = request.form.get("secret", "")
        if secret and Config.DASHBOARD_SECRET and secret == Config.DASHBOARD_SECRET:
            session["mod_logged_in"] = True
            session.permanent = True
            return redirect(url_for("dashboard.index"))
        flash("Invalid secret key. Please try again.", "error")
    return render_template("dashboard/login.html")


@dashboard.route("/logout")
def logout():
    session.pop("mod_logged_in", None)
    return redirect(url_for("dashboard.login"))


@dashboard.route("/")
@login_required
def index():
    db = get_db()
    try:
        total_visits = db.execute("SELECT COUNT(*) FROM island_visits").fetchone()[0]
        total_warnings = db.execute("SELECT COUNT(*) FROM warnings").fetchone()[0]
        recent_raw = db.execute(
            "SELECT ign, destination, authorized, timestamp "
            "FROM island_visits ORDER BY timestamp DESC LIMIT 10"
        ).fetchall()
    except Exception:
        total_visits = total_warnings = 0
        recent_raw = []
    finally:
        db.close()

    recent = [
        {
            "ign": r["ign"],
            "destination": r["destination"],
            "authorized": bool(r["authorized"]),
            "timestamp": str(r["timestamp"]),
        }
        for r in recent_raw
    ]

    free_count = vip_count = 0
    try:
        if Config.DIR_FREE and os.path.exists(Config.DIR_FREE):
            free_count = sum(1 for e in os.scandir(Config.DIR_FREE) if e.is_dir())
        if Config.DIR_VIP and os.path.exists(Config.DIR_VIP):
            vip_count = sum(1 for e in os.scandir(Config.DIR_VIP) if e.is_dir())
    except Exception:
        pass

    return render_template(
        "dashboard/index.html",
        total_visits=total_visits,
        total_warnings=total_warnings,
        recent=recent,
        free_count=free_count,
        vip_count=vip_count,
    )


@dashboard.route("/islands")
@login_required
def islands():
    islands_list = _collect_islands()
    db = get_db()
    try:
        rows = db.execute(
            "SELECT name, category, theme, notes FROM island_metadata"
        ).fetchall()
        meta_map = {r["name"]: dict(r) for r in rows}
    except Exception:
        meta_map = {}
    finally:
        db.close()

    for isl in islands_list:
        m = meta_map.get(isl["name"], {})
        isl["category"] = m.get("category", "public")
        isl["theme"] = m.get("theme", "teal")
        isl["notes"] = m.get("notes", "")

    return render_template("dashboard/islands.html", islands=islands_list)


@dashboard.route("/islands/<name>", methods=["GET", "POST"])
@login_required
def island_detail(name):
    upper = name.upper()

    # Locate the island directory
    island_path = island_type = None
    for directory, itype in [(Config.DIR_FREE, "Free"), (Config.DIR_VIP, "VIP")]:
        if not directory:
            continue
        candidate = os.path.join(directory, upper)
        if not os.path.isdir(candidate):
            candidate = os.path.join(directory, name)
        if os.path.isdir(candidate):
            island_path, island_type = candidate, itype
            break

    if not island_path:
        flash(f'Island "{upper}" not found.', "error")
        return redirect(url_for("dashboard.islands"))

    if request.method == "POST":
        dodo = request.form.get("dodo", "").strip()
        visitors = request.form.get("visitors", "").strip()
        category = request.form.get("category", "public")
        theme = request.form.get("theme", "teal")
        notes = request.form.get("notes", "").strip()

        errors = []
        if category not in ALLOWED_CATEGORIES:
            errors.append("Invalid category.")
        if theme not in ALLOWED_THEMES:
            errors.append("Invalid theme.")

        if errors:
            for e in errors:
                flash(e, "error")
        else:
            if dodo:
                _write_file(island_path, "Dodo.txt", dodo)
            if visitors:
                _write_file(island_path, "Visitors.txt", visitors)

            db = get_db()
            try:
                db.execute(
                    """
                    INSERT INTO island_metadata (name, category, theme, notes, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(name) DO UPDATE SET
                        category   = excluded.category,
                        theme      = excluded.theme,
                        notes      = excluded.notes,
                        updated_at = excluded.updated_at
                    """,
                    (upper, category, theme, notes, datetime.now(timezone.utc).isoformat()),
                )
                db.commit()
            finally:
                db.close()

            flash(f'Island "{upper}" updated successfully.', "success")
            return redirect(url_for("dashboard.islands"))

    db = get_db()
    try:
        meta = db.execute(
            "SELECT * FROM island_metadata WHERE name = ?", (upper,)
        ).fetchone()
    finally:
        db.close()

    island = {
        "name": upper,
        "type": island_type,
        "dodo": _read_file(island_path, "Dodo.txt") or "",
        "visitors": _read_file(island_path, "Visitors.txt") or "",
        "category": meta["category"] if meta else "public",
        "theme": meta["theme"] if meta else "teal",
        "notes": meta["notes"] if meta else "",
        "updated_at": meta["updated_at"] if meta else None,
    }

    return render_template(
        "dashboard/island_detail.html",
        island=island,
        allowed_categories=ALLOWED_CATEGORIES,
        allowed_themes=ALLOWED_THEMES,
        allowed_statuses=ALLOWED_STATUSES,
    )


@dashboard.route("/logs")
@login_required
def logs():
    page = request.args.get("page", 1, type=int)
    per_page = 25
    island_filter = request.args.get("island", "").strip()
    authorized_filter = request.args.get("authorized", "")
    log_type = request.args.get("type", "flights")  # "flights" or "warnings"

    db = get_db()
    try:
        if log_type == "warnings":
            conditions, params = [], []
            where = _where_clause(conditions)
            total = db.execute(
                f"SELECT COUNT(*) FROM warnings {where}", params
            ).fetchone()[0]
            rows = db.execute(
                f"SELECT w.*, iv.ign, iv.destination "
                f"FROM warnings w "
                f"LEFT JOIN island_visits iv ON w.visit_id = iv.id "
                f"{where} ORDER BY w.timestamp DESC LIMIT ? OFFSET ?",
                params + [per_page, (page - 1) * per_page],
            ).fetchall()
            entries = [
                {
                    "user_id": r["user_id"],
                    "reason": r["reason"],
                    "mod_id": r["mod_id"],
                    "timestamp": str(r["timestamp"]),
                    "ign": r["ign"],
                    "destination": r["destination"],
                }
                for r in rows
            ]
        else:
            conditions, params = [], []
            if island_filter:
                conditions.append("destination LIKE ?")
                params.append(f"%{island_filter}%")
            if authorized_filter in ("0", "1"):
                conditions.append("authorized = ?")
                params.append(int(authorized_filter))
            where = _where_clause(conditions)
            total = db.execute(
                f"SELECT COUNT(*) FROM island_visits {where}", params
            ).fetchone()[0]
            rows = db.execute(
                f"SELECT * FROM island_visits {where} "
                f"ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                params + [per_page, (page - 1) * per_page],
            ).fetchall()
            entries = [
                {
                    "id": r["id"],
                    "ign": r["ign"],
                    "origin_island": r["origin_island"],
                    "destination": r["destination"],
                    "authorized": bool(r["authorized"]),
                    "timestamp": _ts_to_str(r["timestamp"]),
                }
                for r in rows
            ]
    except Exception:
        total, entries = 0, []
    finally:
        db.close()

    return render_template(
        "dashboard/logs.html",
        entries=entries,
        page=page,
        per_page=per_page,
        total=total,
        total_pages=max(1, (total + per_page - 1) // per_page),
        island_filter=island_filter,
        authorized_filter=authorized_filter,
        log_type=log_type,
    )


@dashboard.route("/analytics")
@login_required
def analytics():
    db = get_db()
    try:
        top_islands = [
            dict(r)
            for r in db.execute(
                "SELECT destination, COUNT(*) AS visit_count "
                "FROM island_visits GROUP BY destination "
                "ORDER BY visit_count DESC LIMIT 10"
            ).fetchall()
        ]
        top_travelers = [
            dict(r)
            for r in db.execute(
                "SELECT ign, COUNT(*) AS visit_count "
                "FROM island_visits GROUP BY ign "
                "ORDER BY visit_count DESC LIMIT 10"
            ).fetchall()
        ]
        visits_by_day = [
            dict(r)
            for r in db.execute(
                "SELECT DATE(timestamp, 'unixepoch') AS day, COUNT(*) AS count "
                "FROM island_visits "
                "WHERE timestamp > strftime('%s','now','-7 days') "
                "GROUP BY day ORDER BY day"
            ).fetchall()
        ]
        auth_raw = db.execute(
            "SELECT authorized, COUNT(*) AS count FROM island_visits GROUP BY authorized"
        ).fetchall()
    except Exception:
        top_islands = top_travelers = visits_by_day = []
        auth_raw = []
    finally:
        db.close()

    auth_map = {r["authorized"]: r["count"] for r in auth_raw}
    auth_stats = {
        "authorized": auth_map.get(1, 0),
        "unauthorized": auth_map.get(0, 0),
    }

    return render_template(
        "dashboard/analytics.html",
        top_islands=top_islands,
        top_travelers=top_travelers,
        visits_by_day=visits_by_day,
        auth_stats=auth_stats,
    )


# ===========================================================================
# JSON CRUD API  (Bearer token OR active browser session)
# ===========================================================================

@dashboard.route("/api/islands", methods=["GET"])
@api_auth_required
def api_islands_list():
    """List all islands with filesystem data + stored metadata."""
    islands_list = _collect_islands()
    db = get_db()
    try:
        rows = db.execute("SELECT * FROM island_metadata").fetchall()
        meta_map = {r["name"]: dict(r) for r in rows}
    except Exception:
        meta_map = {}
    finally:
        db.close()

    result = []
    for isl in islands_list:
        m = meta_map.get(isl["name"], {})
        result.append({
            "name": isl["name"],
            "type": isl["type"],
            "dodo": isl["dodo"],
            "visitors": isl["visitors"],
            "category": m.get("category", "public"),
            "theme": m.get("theme", "teal"),
            "notes": m.get("notes", ""),
            "updated_at": m.get("updated_at"),
        })
    return jsonify(result)


@dashboard.route("/api/islands", methods=["POST"])
@api_auth_required
def api_island_create():
    """Create or upsert island metadata."""
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip().upper()
    category = data.get("category", "public")
    theme = data.get("theme", "teal")
    notes = data.get("notes", "")

    if not name:
        return jsonify({"error": "name is required"}), 400
    if category not in ALLOWED_CATEGORIES:
        return jsonify({"error": f"category must be one of {ALLOWED_CATEGORIES}"}), 400
    if theme not in ALLOWED_THEMES:
        return jsonify({"error": f"theme must be one of {ALLOWED_THEMES}"}), 400

    db = get_db()
    try:
        db.execute(
            """
            INSERT INTO island_metadata (name, category, theme, notes, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                category   = excluded.category,
                theme      = excluded.theme,
                notes      = excluded.notes,
                updated_at = excluded.updated_at
            """,
            (name, category, theme, notes, datetime.now(timezone.utc).isoformat()),
        )
        db.commit()
    finally:
        db.close()

    return jsonify({"status": "ok", "name": name}), 201


@dashboard.route("/api/islands/<name>", methods=["GET"])
@api_auth_required
def api_island_get(name):
    """Get metadata for a single island."""
    upper = name.upper()
    db = get_db()
    try:
        meta = db.execute(
            "SELECT * FROM island_metadata WHERE name = ?", (upper,)
        ).fetchone()
    finally:
        db.close()

    if not meta:
        return jsonify({"name": upper, "category": "public", "theme": "teal", "notes": "", "updated_at": None})
    return jsonify(dict(meta))


@dashboard.route("/api/islands/<name>", methods=["PUT"])
@api_auth_required
def api_island_update(name):
    """Update metadata for a single island."""
    upper = name.upper()
    data = request.get_json(silent=True) or {}
    category = data.get("category", "public")
    theme = data.get("theme", "teal")
    notes = data.get("notes", "")

    if category not in ALLOWED_CATEGORIES:
        return jsonify({"error": f"category must be one of {ALLOWED_CATEGORIES}"}), 400
    if theme not in ALLOWED_THEMES:
        return jsonify({"error": f"theme must be one of {ALLOWED_THEMES}"}), 400

    db = get_db()
    try:
        db.execute(
            """
            INSERT INTO island_metadata (name, category, theme, notes, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                category   = excluded.category,
                theme      = excluded.theme,
                notes      = excluded.notes,
                updated_at = excluded.updated_at
            """,
            (upper, category, theme, notes, datetime.now(timezone.utc).isoformat()),
        )
        db.commit()
    finally:
        db.close()

    return jsonify({"status": "ok", "name": upper})


@dashboard.route("/api/islands/<name>", methods=["DELETE"])
@api_auth_required
def api_island_delete(name):
    """Delete stored metadata for an island (does not touch the filesystem)."""
    upper = name.upper()
    db = get_db()
    try:
        db.execute("DELETE FROM island_metadata WHERE name = ?", (upper,))
        db.commit()
    finally:
        db.close()
    return jsonify({"status": "deleted", "name": upper})


@dashboard.route("/api/analytics", methods=["GET"])
@api_auth_required
def api_analytics():
    """Return analytics summary as JSON."""
    db = get_db()
    try:
        top_islands = [
            dict(r) for r in db.execute(
                "SELECT destination, COUNT(*) AS visit_count "
                "FROM island_visits GROUP BY destination "
                "ORDER BY visit_count DESC LIMIT 10"
            ).fetchall()
        ]
        top_travelers = [
            dict(r) for r in db.execute(
                "SELECT ign, COUNT(*) AS visit_count "
                "FROM island_visits GROUP BY ign "
                "ORDER BY visit_count DESC LIMIT 10"
            ).fetchall()
        ]
        auth_raw = db.execute(
            "SELECT authorized, COUNT(*) AS count FROM island_visits GROUP BY authorized"
        ).fetchall()
    except Exception:
        top_islands = top_travelers = []
        auth_raw = []
    finally:
        db.close()

    auth_map = {r["authorized"]: r["count"] for r in auth_raw}
    return jsonify({
        "top_islands": top_islands,
        "top_travelers": top_travelers,
        "authorized_visits": auth_map.get(1, 0),
        "unauthorized_visits": auth_map.get(0, 0),
    })


@dashboard.route("/api/logs", methods=["GET"])
@api_auth_required
def api_logs():
    """Return paginated flight-log entries as JSON."""
    page = request.args.get("page", 1, type=int)
    per_page = min(request.args.get("per_page", 25, type=int), 100)
    island_filter = request.args.get("island", "").strip()

    db = get_db()
    try:
        conditions, params = [], []
        if island_filter:
            conditions.append("destination LIKE ?")
            params.append(f"%{island_filter}%")
        where = _where_clause(conditions)
        total = db.execute(
            f"SELECT COUNT(*) FROM island_visits {where}", params
        ).fetchone()[0]
        rows = db.execute(
            f"SELECT * FROM island_visits {where} "
            f"ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            params + [per_page, (page - 1) * per_page],
        ).fetchall()
    except Exception:
        total, rows = 0, []
    finally:
        db.close()

    return jsonify({
        "page": page,
        "per_page": per_page,
        "total": total,
        "entries": [dict(r) for r in rows],
    })
