"""
Flask API Module
Combines all API endpoints:
- Item/Villager Search
- Dodo Code/Island Status
- Patreon Posts
"""

import os
import re
import time
import logging
import sqlite3
import threading
from datetime import datetime, timedelta

import requests
from flask import Flask, jsonify, request
from flask_cors import CORS
from thefuzz import process, fuzz

from werkzeug.middleware.proxy_fix import ProxyFix

from utils.config import Config
from utils.helpers import format_locations_text, parse_locations_json, normalize_text
from api.dashboard import dashboard, init_dashboard_db, get_db, row_to_island_dict, _parse_visitor_value


logger = logging.getLogger("FlaskAPI")

# Initialize Flask app
app = Flask(__name__)
app.secret_key = Config.FLASK_SECRET_KEY
# Trust one level of X-Forwarded-For / X-Forwarded-Proto headers from the
# reverse proxy (nginx, Cloudflare Tunnel, etc.) so that url_for(_external=True)
# produces the correct https:// URL instead of http://.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
CORS(app, resources={r"/*": {"origins": "*"}})

# Register the mod-only web dashboard
app.register_blueprint(dashboard, url_prefix="/dashboard")
init_dashboard_db()

# Suppress Flask/Werkzeug standard logs
logging.getLogger('werkzeug').setLevel(logging.ERROR)


# Patreon cache
patreon_cache = {
    "list": {"data": None, "timestamp": None},
    "posts": {}
}

# Data manager will be set from main.py
data_manager = None

# Guard: prevents multiple concurrent cache-refresh operations
_refresh_lock = threading.Lock()


def set_data_manager(dm):
    """Set the data manager instance"""
    global data_manager
    data_manager = dm

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def extract_image_from_html(html_content):
    """Extract image URL from HTML content"""
    if not html_content:
        return None
    match = re.search(r'<img [^>]*src="([^"]+)"', html_content)
    return match.group(1) if match else None


def process_post_attributes(post_id, attrs):
    """Process Patreon post attributes"""
    image_url = None

    if attrs.get("embed_data"):
        embed = attrs["embed_data"]
        if "image" in embed and "url" in embed["image"]:
            image_url = embed["image"]["url"]
        elif "thumbnail_url" in embed:
            image_url = embed["thumbnail_url"]

    if not image_url:
        image_url = extract_image_from_html(attrs.get("content"))

    return {
        "id": post_id,
        "attributes": {
            "embed_data": attrs.get("embed_data"),
            "title": attrs["title"],
            "content": attrs["content"],
            "published_at": attrs["published_at"],
            "url": attrs["url"],
            "is_public": attrs["is_public"],
            "image": {"large_url": image_url}
        },
        "type": "post"
    }


_file_cache: dict = {}
_file_cache_lock = threading.Lock()
_FILE_CACHE_TTL = 3  # seconds


def get_file_content(folder_path, filename):
    """Read file content safely with caching and retry to reduce file-lock contention.

    The C# SysBot writes to these files with exclusive access (FileShare.None).
    Caching minimises how often the file is opened, and the retry handles the
    brief window where C# holds an exclusive write lock.
    """
    path = os.path.join(folder_path, filename)

    now = time.monotonic()
    with _file_cache_lock:
        cached = _file_cache.get(path)
        if cached is not None:
            content, ts = cached
            if now - ts < _FILE_CACHE_TTL:
                return content

    if not os.path.exists(path):
        return None

    for attempt in range(3):
        try:
            with open(path, 'r', encoding='utf-8-sig') as f:
                content = f.read().strip()
            with _file_cache_lock:
                _file_cache[path] = (content, time.monotonic())
            return content
        except OSError:
            if attempt < 2:
                time.sleep(0.05)
        except Exception:
            break

    # Return stale cache rather than None if the file is still locked
    with _file_cache_lock:
        cached = _file_cache.get(path)
    if cached is not None:
        return cached[0]
    return None


def process_island(entry, island_type):
    """Process island data for Dodo API"""
    name = entry.name.upper()

    raw_dodo = get_file_content(entry.path, "Dodo.txt")
    raw_visitors = _parse_visitor_value(get_file_content(entry.path, "Visitors.txt"))

    status = "ONLINE"
    display_dodo = raw_dodo
    display_visitors = "0/7"

    # Visitor Logic
    if raw_visitors:
        if raw_visitors.upper() == "FULL":
            display_visitors = "FULL"
        elif raw_visitors.isdigit():
            display_visitors = f"{raw_visitors}/7"
        else:
            display_visitors = raw_visitors

    # Dodo/Status Logic
    if island_type == "VIP":
        status = "SUB ONLY"
        display_dodo = "SUB ONLY"
    else:
        if raw_dodo is None:
            status = "OFFLINE"
            display_dodo = "....."
            display_visitors = "0/7"
        elif raw_dodo in ["00000", "-----", ""]:
            status = "REFRESHING"
            display_dodo = "WAIT..."
            display_visitors = "0/7"
        else:
            display_dodo = raw_dodo

    return {
        "name": name,
        "dodo": display_dodo,
        "status": status,
        "type": island_type,
        "visitors": display_visitors
    }


def _build_island_response(entry, island_type, db_island, discord_bot_online=None):
    """Build the enriched island response merging live filesystem data with DB metadata."""
    name = entry.name.upper()

    raw_dodo = get_file_content(entry.path, "Dodo.txt")
    raw_visitors = _parse_visitor_value(get_file_content(entry.path, "Visitors.txt"))

    # Parse visitors as integer
    visitors = 0
    if raw_visitors and raw_visitors.isdigit():
        visitors = int(raw_visitors)

    # Determine live status and dodo_code from filesystem
    if island_type == "VIP":
        status = "SUB ONLY"
        dodo_code = None  # Do not expose dodo code for subscriber-only islands
    elif raw_dodo is None:
        status = "OFFLINE"
        dodo_code = None
    elif raw_dodo in ["00000", "-----", ""]:
        status = "REFRESHING"
        dodo_code = None
    else:
        status = "ONLINE"
        dodo_code = raw_dodo

    return {
        "id":                db_island.get("id", name.lower()),
        "name":              name,
        "cat":               db_island.get("cat", "public"),
        "description":       db_island.get("description", ""),
        "dodo_code":         dodo_code,
        "visitors":          visitors,
        "items":             db_island.get("items", []),
        "map_url":           db_island.get("map_url"),
        "seasonal":          db_island.get("seasonal", ""),
        "status":            status,
        "theme":             db_island.get("theme", "teal"),
        "type":              db_island.get("type", ""),
        "updated_at":        db_island.get("updated_at"),
        "discord_bot_online": discord_bot_online,
    }

# ============================================================================
# ISLAND METADATA CRUD (separate from /api/islands Dodo-status endpoint)
# ============================================================================

ALLOWED_CATEGORIES = {"public", "member"}
ALLOWED_THEMES = {"pink", "teal", "purple", "gold"}
ALLOWED_STATUSES = {"ONLINE", "SUB ONLY", "REFRESHING", "OFFLINE"}

# ============================================================================
# API ROUTES
# ============================================================================

@app.route('/')
def home():
    """API home with endpoint info"""
    return jsonify({
        "status": "online",
        "message": "ChoBot API - All systems operational",
        "endpoints": {
            "items": "/find, /api/find",
            "villagers": "/villager, /api/villager, /api/villagers/list",
            "islands": "/api/islands",
            "patreon": "/api/patreon/posts, /api/patreon/posts/<id>",
            "status": "/status",
            "refresh": "/api/refresh (POST)",
            "health": "/health"
        },
        "data_freshness": {
            "islands": (
                f"Near-real-time — dodo codes and visitor counts are read directly from "
                f"island bot files, cached for up to {_FILE_CACHE_TTL} seconds."
            ),
            "items_villagers": (
                "Refreshed from Google Sheets on a scheduled interval. "
                "See /health for refresh_interval_seconds and next_update."
            ),
        },
    })

@app.route('/health')
@app.route('/api/health')
def health():
    """Health check endpoint for monitoring"""
    with data_manager.lock:
        cache_count = len(data_manager.cache)
        last_update = data_manager.last_update

    is_healthy = cache_count > 0 and last_update is not None

    refresh_interval_seconds = int(data_manager.cache_refresh_hours * 3600)
    if last_update is not None:
        next_update = (last_update + timedelta(seconds=refresh_interval_seconds)).isoformat()
    else:
        next_update = None

    response = {
        "status": "healthy" if is_healthy else "degraded",
        "timestamp": datetime.now().isoformat(),
        "cache": {
            "items": cache_count,
            "last_update": last_update.isoformat() if last_update else None,
            "refresh_interval_seconds": refresh_interval_seconds,
            "next_update": next_update,
        },
        "islands": {
            "file_cache_ttl_seconds": _FILE_CACHE_TTL,
        },
    }

    status_code = 200 if is_healthy else 503
    return jsonify(response), status_code

# --- ITEM SEARCH ROUTES ---

@app.route('/find')
def find_item():
    """Text response for item search"""
    user = request.args.get('user', 'User')
    query = normalize_text(request.args.get('q', ''))

    if not query:
        return f"Hey {user}, type !find <item name> to search."

    with data_manager.lock:
        cache = data_manager.cache

    found_locs = cache.get(query)

    if found_locs:
        final_msg = format_locations_text(found_locs)
        return f"Hey {user}, I found {query.upper()} {final_msg}"

    matches = process.extract(query, list(cache.keys()), limit=5, scorer=fuzz.token_set_ratio)
    valid_suggestions = list(set([m[0] for m in matches if m[1] > 75]))

    if valid_suggestions:
        suggestions_str = ", ".join(valid_suggestions)
        return f"Hey {user}, I couldn't find \"{query}\" - Did you mean: {suggestions_str}? If not, try !orderbot."

    return f"Hey {user}, I couldn't find \"{query}\" or anything similar. Please check spelling."


@app.route('/api/find')
def api_find_item():
    """JSON response for item search"""
    user = request.args.get('user', 'User')
    query = normalize_text(request.args.get('q', ''))

    if not query:
        return jsonify({"found": False, "message": f"Hey {user}, type !find <item name> to search."})

    with data_manager.lock:
        cache = data_manager.cache

    found_locs = cache.get(query)

    if found_locs:
        free, sub = parse_locations_json(found_locs)
        final_msg = format_locations_text(found_locs)
        return jsonify({
            "found": True,
            "query": query,
            "results": {"free": free, "sub": sub},
            "suggestions": [],
            "message": f"Hey {user}, I found {query.upper()} {final_msg}"
        })

    matches = process.extract(query, list(cache.keys()), limit=5, scorer=fuzz.token_set_ratio)
    valid_suggestions = list(set([m[0] for m in matches if m[1] > 75]))

    if valid_suggestions:
        return jsonify({
            "found": False,
            "query": query,
            "suggestions": valid_suggestions,
            "message": f"Hey {user}, I couldn't find \"{query}\" - Did you mean: {', '.join(valid_suggestions)}?"
        })

    return jsonify({
        "found": False,
        "query": query,
        "suggestions": [],
        "message": f"Hey {user}, I couldn't find \"{query}\" or anything similar."
    })


# --- VILLAGER SEARCH ROUTES ---

@app.route('/villager')
def find_villager():
    """Text response for villager search"""
    user = request.args.get('user', 'User')
    query = normalize_text(request.args.get('q', ''))

    if not query:
        return f"Hey {user}, type !villager <n> to search."

    villager_map = data_manager.get_villagers([Config.VILLAGERS_DIR, Config.TWITCH_VILLAGERS_DIR])
    found_locs = villager_map.get(query)

    if found_locs:
        final_msg = format_locations_text(found_locs)
        return f"Hey {user}, I found villager {query.upper()} {final_msg}"

    matches = process.extract(query, list(villager_map.keys()), limit=3, scorer=fuzz.token_set_ratio)
    valid_suggestions = list(set([m[0] for m in matches if m[1] > 75]))

    if valid_suggestions:
        suggestions_str = ", ".join(valid_suggestions)
        return f"Hey {user}, I couldn't find villager \"{query}\" - Did you mean: {suggestions_str}?"

    return f"Hey {user}, I couldn't find a villager named \"{query}\"."


@app.route('/api/villager')
def api_find_villager():
    """JSON response for villager search"""
    user = request.args.get('user', 'User')
    query = normalize_text(request.args.get('q', ''))

    if not query:
        return jsonify({"found": False, "message": f"Hey {user}, type !villager <n> to search."})

    villager_map = data_manager.get_villagers([Config.VILLAGERS_DIR, Config.TWITCH_VILLAGERS_DIR])
    found_locs = villager_map.get(query)

    if found_locs:
        free, sub = parse_locations_json(found_locs)
        final_msg = format_locations_text(found_locs)
        return jsonify({
            "found": True,
            "query": query,
            "results": {"free": free, "sub": sub},
            "suggestions": [],
            "message": f"Hey {user}, I found villager {query.upper()} {final_msg}"
        })

    matches = process.extract(query, list(villager_map.keys()), limit=3, scorer=fuzz.token_set_ratio)
    valid_suggestions = list(set([m[0] for m in matches if m[1] > 75]))

    if valid_suggestions:
        return jsonify({
            "found": False,
            "query": query,
            "suggestions": valid_suggestions,
            "message": f"Hey {user}, I couldn't find villager \"{query}\" - Did you mean: {', '.join(valid_suggestions)}?"
        })

    return jsonify({
        "found": False,
        "query": query,
        "suggestions": [],
        "message": f"Hey {user}, I couldn't find a villager named \"{query}\"."
    })


@app.route('/api/villagers/list')
def api_list_villagers_by_island():
    """List all villagers grouped by island"""
    villager_map = data_manager.get_villagers([Config.VILLAGERS_DIR, Config.TWITCH_VILLAGERS_DIR])
    island_manifest = {}

    for villager_name, locations in villager_map.items():
        loc_list = locations.split(", ")
        for loc in loc_list:
            if loc not in island_manifest:
                island_manifest[loc] = []
            island_manifest[loc].append(villager_name.title())

    for loc in island_manifest:
        island_manifest[loc].sort()

    return jsonify({
        "timestamp": datetime.now().isoformat(),
        "total_islands": len(island_manifest),
        "islands": island_manifest
    })


# --- DODO CODE / ISLAND STATUS ROUTES ---

@app.route('/api/islands', methods=['GET'])
def get_islands():
    """Get all island statuses and Dodo codes with full metadata."""
    # Load island metadata from DB, keyed by uppercase name
    db_map = {}
    discord_status = {}
    db = get_db()
    try:
        rows = db.execute(
            "SELECT id, name, cat, description, items, map_url, seasonal, theme, type, updated_at "
            "FROM islands ORDER BY name"
        ).fetchall()
        for row in rows:
            isl = row_to_island_dict(dict(row))
            if isl.get("name"):
                db_map[isl["name"].upper()] = isl
        # Load Discord bot presence data
        bot_rows = db.execute("SELECT island_id, is_online FROM island_bot_status").fetchall()
        for r in bot_rows:
            discord_status[r["island_id"]] = bool(r["is_online"])
    except sqlite3.Error:
        logger.exception("Failed to load island metadata from DB for /api/islands")
    finally:
        db.close()

    results = []

    if os.path.exists(Config.DIR_FREE):
        with os.scandir(Config.DIR_FREE) as entries:
            for entry in entries:
                if entry.is_dir():
                    name = entry.name.upper()
                    results.append(_build_island_response(
                        entry, "Free", db_map.get(name, {}),
                        discord_status.get(name.lower()),
                    ))

    if os.path.exists(Config.DIR_VIP):
        with os.scandir(Config.DIR_VIP) as entries:
            for entry in entries:
                if entry.is_dir():
                    name = entry.name.upper()
                    results.append(_build_island_response(
                        entry, "VIP", db_map.get(name, {}),
                        discord_status.get(name.lower()),
                    ))

    results.sort(key=lambda x: x['name'])
    return jsonify({
        "meta": {
            "timestamp": datetime.now().isoformat(),
            "cache_ttl_seconds": _FILE_CACHE_TTL,
            "note": (
                f"Dodo codes and visitor counts are read directly from files written by "
                f"the C# island bot. Each file read is cached for up to "
                f"{_FILE_CACHE_TTL} seconds, so data is near-real-time."
            ),
        },
        "data": results,
    })


# --- PATREON ROUTES ---

@app.route("/api/patreon/posts", methods=["GET"])
def get_patreon_posts():
    """Get recent Patreon posts (cached 15 min)"""
    now = datetime.now()
    if patreon_cache["list"]["data"] and patreon_cache["list"]["timestamp"]:
        if (now - patreon_cache["list"]["timestamp"]) < timedelta(minutes=15):
            return jsonify(patreon_cache["list"]["data"])

    url = f"https://www.patreon.com/api/oauth2/v2/campaigns/{Config.PATREON_CAMPAIGN_ID}/posts"
    headers = {"Authorization": f"Bearer {Config.PATREON_TOKEN}"}
    params = {
        "fields[post]": "title,content,published_at,url,is_public,embed_data,embed_url",
        "sort": "-published_at",
        "page[count]": 10
    }

    try:
        response = requests.get(url, headers=headers, params=params, timeout=20)
        if not response.ok:
            return jsonify({"error": "Patreon API Error", "details": response.text}), response.status_code

        raw_data = response.json()
        processed_data = [process_post_attributes(p["id"], p["attributes"]) for p in raw_data["data"]]

        result = {"data": processed_data}
        patreon_cache["list"] = {"data": result, "timestamp": now}
        return jsonify(result)

    except Exception as e:
        return jsonify({"error": "Server error", "details": str(e)}), 500


@app.route("/api/patreon/posts/<post_id>", methods=["GET"])
def get_single_post(post_id):
    """Get a specific Patreon post (cached 15 min)"""
    now = datetime.now()

    if post_id in patreon_cache["posts"]:
        cached_post = patreon_cache["posts"][post_id]
        if (now - cached_post["timestamp"]) < timedelta(minutes=15):
            return jsonify(cached_post["data"])

    url = f"https://www.patreon.com/api/oauth2/v2/posts/{post_id}"
    headers = {"Authorization": f"Bearer {Config.PATREON_TOKEN}"}
    params = {"fields[post]": "title,content,published_at,url,is_public,embed_data,embed_url"}

    try:
        response = requests.get(url, headers=headers, params=params, timeout=20)
        if not response.ok:
            return jsonify({"error": "Post not found or API error", "details": response.text}), response.status_code

        raw_data = response.json()
        processed_post = process_post_attributes(raw_data["data"]["id"], raw_data["data"]["attributes"])

        result = {"data": processed_post}
        patreon_cache["posts"][post_id] = {"data": result, "timestamp": now}
        return jsonify(result)

    except Exception as e:
        return jsonify({"error": "Server error", "details": str(e)}), 500


# --- STATUS ROUTE ---
@app.route('/status')
def status():
    """Get bot status"""
    with data_manager.lock:
        count = len(data_manager.cache)
        last_up = data_manager.last_update.strftime("%H:%M:%S") if data_manager.last_update else "Loading..."
    return f"Items: {count} | Last Update: {last_up}"


@app.route('/api/refresh', methods=['POST'])
def api_refresh():
    """Manually trigger a cache refresh from Google Sheets"""
    if not _refresh_lock.acquire(blocking=False):
        return jsonify({"status": "refresh already in progress"}), 429

    def _run():
        try:
            data_manager.update_cache()
        finally:
            _refresh_lock.release()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return jsonify({"status": "refresh started"}), 202


def run_flask_app(host='0.0.0.0', port=8100):
    """Run Flask app"""
    logger.info(f"[FLASK] Starting API server on {host}:{port}...")
    app.run(host=host, port=port, debug=False, use_reloader=False)