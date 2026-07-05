import os
import re
import logging
from flask import Flask, request, jsonify, send_file, abort
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(name)s - %(message)s")
logger = logging.getLogger("SysBotAgent")

app = Flask(__name__)

VILLAGERS_DIR = os.getenv('VILLAGERS_DIR')
TWITCH_VILLAGERS_DIR = os.getenv('TWITCH_VILLAGERS_DIR')
ORDER_BOT_DIR = os.getenv('ORDER_BOT_DIR') or os.getenv('ORDER_BOR_DIR')
SYSBOT_AGENT_SECRET = os.getenv('SYSBOT_AGENT_SECRET', 'dev-secret')

def require_auth(f):
    def wrapper(*args, **kwargs):
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            return jsonify({'error': 'Unauthorized'}), 401
        token = auth_header.split(' ')[1]
        if token != SYSBOT_AGENT_SECRET:
            return jsonify({'error': 'Forbidden'}), 403
        return f(*args, **kwargs)
    wrapper.__name__ = f.__name__
    return wrapper

@app.route('/api/health')
def health():
    return jsonify({'status': 'ok'})

def get_villagers_local(villagers_dirs):
    paths_to_scan = tuple(sorted(p for p in villagers_dirs if p and os.path.exists(p)))
    if not paths_to_scan:
        return {}
    
    data = {}
    for base_dir in paths_to_scan:
        for root, dirs, files in os.walk(base_dir):
            if "Villagers.txt" in files:
                location_name = os.path.basename(root)
                file_path = os.path.join(root, "Villagers.txt")
                try:
                    with open(file_path, 'rb') as f:
                        raw_content = f.read().decode('utf-8', errors='ignore')
                    raw_content = re.sub(r'Villagers\s+on\s+[^:]+:', '', raw_content, flags=re.IGNORECASE)
                    names_list = re.split(r'[,\n\r]+', raw_content)
                    
                    for name in names_list:
                        clean_name = name.strip()
                        if not clean_name or len(clean_name) > 30:
                            continue
                        if clean_name in ["Ren?E", "Ren?e"]:
                            clean_name = "Renée"
                        key = clean_name.lower()
                        if key in data:
                            current_locs = data[key].split(", ")
                            if location_name not in current_locs:
                                data[key] += f", {location_name}"
                        else:
                            data[key] = location_name
                except Exception as e:
                    logger.error(f"Error reading villagers at {location_name}: {e}")
    return data

@app.route('/api/villagers')
@require_auth
def api_villagers():
    dirs = [VILLAGERS_DIR, TWITCH_VILLAGERS_DIR, ORDER_BOT_DIR]
    data = get_villagers_local(dirs)
    return jsonify(data)

def _read_file_safe(folder_path, filename):
    try:
        with open(os.path.join(folder_path, filename), "r", encoding="utf-8-sig") as fh:
            return fh.read().strip()
    except (FileNotFoundError, IOError, UnicodeDecodeError):
        return None

@app.route('/api/islands/status')
@require_auth
def api_islands_status():
    categories = {
        "VIP": VILLAGERS_DIR,
        "Free": TWITCH_VILLAGERS_DIR,
        "Order": ORDER_BOT_DIR
    }
    status_data = {"VIP": {}, "Free": {}, "Order": {}}
    
    for cat_name, base_dir in categories.items():
        if not base_dir or not os.path.exists(base_dir):
            continue
        try:
            # Special logic for order bot direct files
            if cat_name == "Order":
                direct_order_files = [
                    os.path.join(base_dir, "Dodo.txt"),
                    os.path.join(base_dir, "Visitors.txt"),
                    os.path.join(base_dir, "Villagers.txt"),
                ]
                order_name = os.getenv('ORDER_BOT_ISLAND', os.path.basename(base_dir))
                if any(os.path.exists(path) for path in direct_order_files):
                    status_data[cat_name][order_name] = {
                        "dodo": _read_file_safe(base_dir, "Dodo.txt"),
                        "visitors": _read_file_safe(base_dir, "Visitors.txt"),
                        "turnip": _read_file_safe(base_dir, "Turnip.txt"),
                        "crash": _read_file_safe(base_dir, "Crash.txt"),
                        "status": _read_file_safe(base_dir, "Status.txt"),
                        "has_map": os.path.exists(os.path.join(base_dir, "Map.png")),
                        "is_direct": True
                    }
                    
            for item in os.listdir(base_dir):
                folder_path = os.path.join(base_dir, item)
                if not os.path.isdir(folder_path):
                    continue
                
                island_data = {
                    "dodo": _read_file_safe(folder_path, "Dodo.txt"),
                    "visitors": _read_file_safe(folder_path, "Visitors.txt"),
                    "turnip": _read_file_safe(folder_path, "Turnip.txt"),
                    "crash": _read_file_safe(folder_path, "Crash.txt"),
                    "status": _read_file_safe(folder_path, "Status.txt"),
                    "has_map": os.path.exists(os.path.join(folder_path, "Map.png")),
                    "is_direct": False
                }
                
                status_data[cat_name][item] = island_data
        except Exception as e:
            logger.error(f"Error scanning {base_dir}: {e}")
            
    return jsonify(status_data)

@app.route('/api/islands/map/<path:island_dir>')
@require_auth
def api_island_map(island_dir):
    dirs = [VILLAGERS_DIR, TWITCH_VILLAGERS_DIR, ORDER_BOT_DIR]
    for base_dir in dirs:
        if not base_dir or not os.path.exists(base_dir):
            continue
        folder_path = os.path.join(base_dir, island_dir)
        map_path = os.path.join(folder_path, "Map.png")
        if os.path.exists(map_path):
            return send_file(map_path, mimetype='image/png')
    abort(404)

def run_agent(host='0.0.0.0', port=8101):
    logger.info(f"Starting SysBot Local Agent on {host}:{port}")
    try:
        from waitress import serve
        serve(app, host=host, port=port)
    except ImportError:
        logger.warning("waitress not found, falling back to Flask development server.")
        app.run(host=host, port=port, threaded=True)

if __name__ == '__main__':
    run_agent()
