"""
Configuration and Island Port Resolver for SysBot Bridge.
Maps all 48 SysBot.ACNHOrders island instances to their local HTTP ports (8201..8248).
"""

import os
import re
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

_BRIDGE_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _BRIDGE_DIR.parent

# Load configuration from .env files (.env in sysbot_bridge, root .env, or fallback to .env.example)
if load_dotenv is not None:
    if (_BRIDGE_DIR / ".env").is_file():
        load_dotenv(_BRIDGE_DIR / ".env")
    if (_REPO_ROOT / ".env").is_file():
        load_dotenv(_REPO_ROOT / ".env")
    if not os.getenv("SYSBOT_API_KEY") and (_BRIDGE_DIR / ".env.example").is_file():
        load_dotenv(_BRIDGE_DIR / ".env.example")
else:
    for env_path in (_BRIDGE_DIR / ".env", _REPO_ROOT / ".env", _BRIDGE_DIR / ".env.example"):
        if env_path.is_file():
            try:
                for line in env_path.read_text(encoding="utf-8").splitlines():
                    clean_line = line.strip()
                    if clean_line and not clean_line.startswith("#") and "=" in clean_line:
                        k, v = clean_line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip("'\"")
                        if k not in os.environ:
                            os.environ[k] = v
            except Exception:
                pass

# Bridge Server Host & Port (Default to 127.0.0.1 for local-only / Cloudflare Tunnel safety)
BRIDGE_HOST = os.getenv("BRIDGE_HOST", "127.0.0.1").strip()
BRIDGE_PORT = int(os.getenv("BRIDGE_PORT", "8200"))

# Secret API key required for incoming requests (set to protect Cloudflare Tunnel access)
SYSBOT_API_KEY = os.getenv("SYSBOT_API_KEY", "").strip()

# Optional Cloudflare Access Service Token validation (secondary security layer)
CF_ACCESS_CLIENT_ID = os.getenv("CF_ACCESS_CLIENT_ID", "").strip()
CF_ACCESS_CLIENT_SECRET = os.getenv("CF_ACCESS_CLIENT_SECRET", "").strip()

# Optional CORS origin (leave empty to disable CORS headers entirely)
CORS_ORIGIN = os.getenv("CORS_ORIGIN", "").strip()

# Base port for sequential allocation (Default: 8201)
SYSBOT_BASE_PORT = int(os.getenv("SYSBOT_BASE_PORT", "8201"))
_RAW_LOCAL_HOST = os.getenv("SYSBOT_LOCAL_HOST", "127.0.0.1").strip()
# Force numeric IPv4 to prevent Windows resolving localhost to ::1
SYSBOT_LOCAL_HOST = "127.0.0.1" if _RAW_LOCAL_HOST.lower() in ("localhost", "127.0.0.1", "") else _RAW_LOCAL_HOST

# Island Definitions matching cluster layout
SUB_ISLANDS: list[str] = [
    "Hiraya", "Tagumpay", "Bahaghari", "Alapaap", "Bituin",
    "Lakan", "Galak", "Aruga", "Likha", "Dalisay",
    "Marahuyo", "Diwa", "Bonita", "Gabay", "Dakila",
    "Kalangitan", "Malaya", "Pangarap", "Giliw", "Adhika",
]

FREE_ISLANDS: list[str] = [
    "Kakanggata", "Kalawakan", "Kundiman", "Kilig", "Bathala",
    "Dalangin", "Gunita", "Kaulayaw", "Tala", "Sinagtala",
    "Tinig", "Tadhana", "Maharlika", "Pagsamo", "Harana",
    "Pagsuyo", "Matahom", "Paraluman", "Marilag", "Babaylan",
    "Amihan", "Silakbo", "Banaag", "Dangal", "Kariktan",
    "Sinag", "Giting",
]

ORDER_ISLANDS: list[str] = ["Sinta"]

ALL_ISLANDS: list[str] = SUB_ISLANDS + FREE_ISLANDS + ORDER_ISLANDS

# Pre-computed mapping: lower_name -> port
_ISLAND_PORT_MAP: dict[str, int] = {}
for idx, name in enumerate(ALL_ISLANDS):
    clean = name.strip().lower()
    env_override = os.getenv(f"SYSBOT_PORT_{name.strip().upper()}")
    if env_override and env_override.isdigit():
        _ISLAND_PORT_MAP[clean] = int(env_override)
    else:
        _ISLAND_PORT_MAP[clean] = SYSBOT_BASE_PORT + idx


def normalize_island_name(island_name: str) -> str:
    """Normalize input island name string."""
    clean = re.sub(r"^sub\s+island\s+", "", (island_name or "").strip(), flags=re.IGNORECASE)
    return clean.strip().lower()


def get_island_port(island_name: str) -> int | None:
    """Retrieve the local port for a given island."""
    norm = normalize_island_name(island_name)
    if norm.isdigit():
        p = int(norm)
        if 1024 <= p <= 65535:
            return p
    return _ISLAND_PORT_MAP.get(norm)


def get_island_local_url(island_name: str, path: str = "") -> str | None:
    """Resolve full local destination URL for an island."""
    port = get_island_port(island_name)
    if not port:
        return None
    sub = path.lstrip("/")
    return f"http://{SYSBOT_LOCAL_HOST}:{port}/{sub}" if sub else f"http://{SYSBOT_LOCAL_HOST}:{port}"


def get_all_island_ports() -> dict[str, int]:
    """Return dictionary of canonical island name -> local port."""
    result = {}
    for name in ALL_ISLANDS:
        result[name] = _ISLAND_PORT_MAP[name.lower()]
    return result
