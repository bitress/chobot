"""
SysBot Multi-Island Local Gateway & Cloudflare Tunnel Bridge.
Runs on the local PC alongside 48 SysBot.ACNHOrders instances.
Exposes a single secure HTTP endpoint for Cloudflare Tunnel to connect VPS Chobot to all local bots.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import aiohttp
import yarl
from aiohttp import web

# Ensure repository root is in sys.path to allow standalone or package execution
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from sysbot_bridge.config import (  # noqa: E402
    ALL_ISLANDS,
    BRIDGE_HOST,
    BRIDGE_PORT,
    CF_ACCESS_CLIENT_ID,
    CF_ACCESS_CLIENT_SECRET,
    CORS_ORIGIN,
    SYSBOT_API_KEY,
    SYSBOT_LOCAL_HOST,
    get_all_island_ports,
    get_island_local_url,
    get_island_port,
    normalize_island_name,
)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [SysBotBridge] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("SysBotBridge")

# Insecure mode flag (--insecure CLI argument or SYSBOT_BRIDGE_INSECURE=1 env var)
INSECURE_MODE = (
    "--insecure" in sys.argv
    or os.getenv("SYSBOT_BRIDGE_INSECURE", "").strip().lower() in ("1", "true", "yes")
)

# Use monotonic clock for drift-free uptime calculation
START_TIME = time.monotonic()
CLIENT_SESSION_KEY = web.AppKey("client_session", aiohttp.ClientSession)

# Streaming proxy timeout (no total limit to allow SSE/long-polling, 3s connect, 30s socket idle)
STREAM_TIMEOUT = aiohttp.ClientTimeout(total=None, connect=3.0, sock_read=30.0)

# Hop-by-hop and sensitive headers to strip when forwarding requests and responses
HOP_BY_HOP = {
    "host",
    "content-length",
    "connection",
    "keep-alive",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "proxy-authorization",
    "proxy-authenticate",
}
STRIP_REQ = HOP_BY_HOP | {
    "x-api-key",
    "authorization",
    "cf-access-client-id",
    "cf-access-client-secret",
}
# Content-encoding is stripped because aiohttp auto-decompresses responses
STRIP_RESP = HOP_BY_HOP | {"content-encoding", "content-length"}

# Allowlist of (method, path-prefix) pairs that Chobot needs
ALLOWED_PROXY_PREFIXES: tuple[tuple[str, str], ...] = (
    ("GET", "/api/status"),
    ("GET", "/api/telemetry"),
    ("GET", "/api/gametime"),
    ("GET", "/api/queue"),
    ("GET", "/api/dodo"),
    ("POST", "/api/dodo/refresh"),
    ("GET", "/api/presets"),
    ("GET", "/api/villagers"),
    ("POST", "/api/villagers"),
    ("GET", "/api/events"),
    ("GET", "/api/activity"),
    ("POST", "/api/order"),
    ("GET", "/api/order/status"),
    ("POST", "/api/order/cancel"),
    ("POST", "/api/click"),
    ("POST", "/api/drop"),
    ("POST", "/api/clean"),
    ("POST", "/api/turnips"),
    ("POST", "/api/speak"),
    ("POST", "/api/control"),
    ("GET", "/api/control/guide"),
    ("POST", "/api/requests/pause"),
    ("POST", "/api/requests/resume"),
    ("GET", "/api/screen"),
    ("POST", "/api/screen"),
    ("POST", "/api/detach"),
    ("POST", "/api/togglemashb"),
    ("POST", "/api/togglerefresh"),
    ("POST", "/api/echo"),
    ("POST", "/api/toggleecho"),
    ("GET", "/api/map"),
    ("POST", "/api/map"),
    ("GET", "/api/logs"),
)

# Concurrency semaphores (limit concurrent proxy requests per island to protect Switch HTTP servers)
_ISLAND_SEMAPHORES: dict[str, asyncio.Semaphore] = {
    name.strip().lower(): asyncio.Semaphore(2) for name in ALL_ISLANDS
}


def get_island_semaphore(island_name: str) -> asyncio.Semaphore:
    """Retrieve or allocate per-island concurrency semaphore."""
    clean = normalize_island_name(island_name)
    if clean not in _ISLAND_SEMAPHORES:
        _ISLAND_SEMAPHORES[clean] = asyncio.Semaphore(2)
    return _ISLAND_SEMAPHORES[clean]


# ── Single-Flight Cache for Fan-Out Cluster Endpoints ─────────────────────────

class ClusterCache:
    """Thread-safe, single-flight TTL cache for high-fanout cluster endpoints."""

    def __init__(self, ttl_seconds: float = 2.0) -> None:
        self.ttl = ttl_seconds
        self.lock = asyncio.Lock()
        self.cached_time: float = 0.0
        self.cached_data: dict[str, Any] | None = None

    async def get_or_fetch(self, fetcher: Callable[[], Any]) -> dict[str, Any]:
        now = time.monotonic()
        if self.cached_data is not None and (now - self.cached_time) < self.ttl:
            return self.cached_data

        async with self.lock:
            # Double-check cache after acquiring lock to prevent duplicate fan-outs
            now = time.monotonic()
            if self.cached_data is not None and (now - self.cached_time) < self.ttl:
                return self.cached_data

            data = await fetcher()
            self.cached_data = data
            self.cached_time = time.monotonic()
            return data


_STATUS_CACHE = ClusterCache(ttl_seconds=2.0)
_TELEMETRY_CACHE = ClusterCache(ttl_seconds=2.0)


# ── Middleware ─────────────────────────────────────────────────────────────────

@web.middleware
async def auth_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    """
    Validate credentials with constant-time comparison.
    If INSECURE_MODE is active, all requests bypass authentication.
    Otherwise, only /health is accessible unauthenticated.
    """
    if INSECURE_MODE or request.method == "OPTIONS" or request.path == "/health":
        return await handler(request)

    # 1. Cloudflare Access Service Token validation (if configured)
    if CF_ACCESS_CLIENT_ID and CF_ACCESS_CLIENT_SECRET:
        cf_id = request.headers.get("CF-Access-Client-Id", "").strip()
        cf_secret = request.headers.get("CF-Access-Client-Secret", "").strip()
        if not (
            hmac.compare_digest(cf_id.encode("utf-8"), CF_ACCESS_CLIENT_ID.encode("utf-8"))
            and hmac.compare_digest(cf_secret.encode("utf-8"), CF_ACCESS_CLIENT_SECRET.encode("utf-8"))
        ):
            return web.json_response(
                {"success": False, "error": "Unauthorized: Invalid Cloudflare Access Service Token."},
                status=401,
            )

    # 2. SysBot Bridge API Key validation (header only - query key rejected for security)
    if SYSBOT_API_KEY:
        header_key = request.headers.get("X-API-Key", "").strip()
        auth_header = request.headers.get("Authorization", "").strip()
        bearer_key = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else ""
        token = header_key or bearer_key

        if not token or not hmac.compare_digest(token.encode("utf-8"), SYSBOT_API_KEY.encode("utf-8")):
            return web.json_response(
                {"success": False, "error": "Unauthorized: Missing or invalid SysBot Bridge API key."},
                status=401,
            )
    elif "--insecure" not in sys.argv:
        return web.json_response(
            {"success": False, "error": "Unauthorized: Bridge is locked because SYSBOT_API_KEY is not set."},
            status=500,
        )

    return await handler(request)


@web.middleware
async def cors_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    """Inject restricted CORS headers if CORS_ORIGIN is explicitly configured."""
    if request.method == "OPTIONS":
        resp: web.StreamResponse = web.Response(status=204)
    else:
        try:
            resp = await handler(request)
        except web.HTTPException as exc:
            resp = exc

    if CORS_ORIGIN:
        resp.headers["Access-Control-Allow-Origin"] = CORS_ORIGIN
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = (
            "Content-Type, Authorization, X-API-Key, Accept, CF-Access-Client-Id, CF-Access-Client-Secret"
        )
    return resp


# ── Route Handlers ─────────────────────────────────────────────────────────────

async def handle_health(request: web.Request) -> web.Response:
    """Minimal public liveness probe."""
    return web.json_response({"status": "ok"})


async def handle_root(request: web.Request) -> web.Response:
    """Bridge status and discovery overview (authenticated)."""
    uptime_sec = int(time.monotonic() - START_TIME)
    ports = list(get_all_island_ports().values())
    return web.json_response({
        "service": "SysBot Bridge & Cloudflare Gateway",
        "version": "1.1.0",
        "status": "online",
        "uptime_seconds": uptime_sec,
        "islands_mapped": len(ALL_ISLANDS),
        "port_range": f"{min(ports)}..{max(ports)}" if ports else "",
        "auth_enabled": bool(SYSBOT_API_KEY) and not INSECURE_MODE,
        "insecure_mode": INSECURE_MODE,
        "endpoints": {
            "health": "/health",
            "islands": "/islands",
            "cluster_status": "/cluster/status",
            "cluster_telemetry": "/cluster/telemetry",
            "island_proxy": "/islands/{island}/{path}",
        },
    })


async def handle_islands_list(request: web.Request) -> web.Response:
    """List all mapped islands and their target local ports."""
    return web.json_response({
        "success": True,
        "total": len(ALL_ISLANDS),
        "islands": get_all_island_ports(),
    })


async def fetch_cluster(session: aiohttp.ClientSession, path: str) -> dict[str, dict[str, Any]]:
    """Deduplicated fanout query to all 48 local SysBots concurrently."""
    async def one(name: str) -> tuple[str, dict[str, Any]]:
        url = get_island_local_url(name, path)
        if not url:
            return name, {
                "success": False,
                "unreachable": True,
                "island_name": name,
                "error": "Island port unmapped",
                "is_running": False,
                "is_connected": False,
                "console_stage": "Offline",
            }
        try:
            async with session.get(url) as r:
                data = await r.json(content_type=None)
                if isinstance(data, dict):
                    data.setdefault("island_name", name)
                    return name, data
                return name, {
                    "success": False,
                    "unreachable": True,
                    "island_name": name,
                    "error": "Invalid JSON response",
                }
        except Exception as exc:
            # Fall back to type(exc).__name__ if str(exc) is empty (e.g. TimeoutError)
            err_msg = str(exc) or type(exc).__name__
            return name, {
                "success": False,
                "unreachable": True,
                "island_name": name,
                "error": err_msg,
                "is_running": False,
                "is_connected": False,
                "console_stage": "Offline",
            }

    pairs = await asyncio.gather(*(one(n) for n in ALL_ISLANDS))
    return dict(pairs)


async def handle_cluster_status(request: web.Request) -> web.Response:
    """Cached cluster status endpoint with single-flight deduplication."""
    session: aiohttp.ClientSession = request.app[CLIENT_SESSION_KEY]

    async def _fetch() -> dict[str, Any]:
        results = await fetch_cluster(session, "/api/status")
        online_count = sum(
            1 for d in results.values()
            if d.get("is_running") and d.get("is_connected")
        )
        return {
            "success": True,
            "total": len(ALL_ISLANDS),
            "online_count": online_count,
            "timestamp": int(time.time()),
            "islands": results,
        }

    data = await _STATUS_CACHE.get_or_fetch(_fetch)
    return web.json_response(data)


async def handle_cluster_telemetry(request: web.Request) -> web.Response:
    """Cached cluster telemetry endpoint with single-flight deduplication."""
    session: aiohttp.ClientSession = request.app[CLIENT_SESSION_KEY]

    async def _fetch() -> dict[str, Any]:
        results = await fetch_cluster(session, "/api/telemetry")
        return {
            "success": True,
            "total": len(ALL_ISLANDS),
            "timestamp": int(time.time()),
            "telemetry": results,
        }

    data = await _TELEMETRY_CACHE.get_or_fetch(_fetch)
    return web.json_response(data)


async def proxy_island_request(request: web.Request) -> web.StreamResponse:
    """
    Transparently stream any authorized HTTP request to the designated local SysBot instance.
    Enforces route allowlist, per-island concurrency limits, safe path parsing, and header sanitization.
    """
    island_param = request.match_info.get("island", "")
    subpath = request.match_info.get("subpath", "")

    port = get_island_port(island_param)
    if not port:
        return web.json_response(
            {"success": False, "error": f"Unknown island '{island_param}'. Check /islands for valid names."},
            status=404,
        )

    # 1. Path sanitation and traversal prevention
    segments = [s for s in subpath.split("/") if s]
    if any(s == ".." or "\\" in s for s in segments):
        return web.json_response(
            {"success": False, "error": "Bad Request: Path traversal or invalid segment detected."},
            status=400,
        )

    target_path = "/" + "/".join(segments)

    # 2. Strict route allowlist check
    method = request.method.upper()
    is_allowed = any(
        method == m and (target_path == p or target_path.startswith(p + "/"))
        for m, p in ALLOWED_PROXY_PREFIXES
    )
    if not is_allowed and method != "OPTIONS":
        return web.json_response(
            {"success": False, "error": f"Forbidden: '{method} {target_path}' is not an allowed SysBot proxy route."},
            status=403,
        )

    # 3. Construct upstream URL using yarl to prevent injection
    target_url = yarl.URL.build(
        scheme="http",
        host=SYSBOT_LOCAL_HOST,
        port=port,
        path=target_path,
    )

    session: aiohttp.ClientSession = request.app[CLIENT_SESSION_KEY]

    # 4. Strip hop-by-hop and authentication credentials from upstream request
    forward_headers = {
        h: val
        for h, val in request.headers.items()
        if h.lower() not in STRIP_REQ
    }

    # 5. Remove api_key from query parameters if present
    params = {k: v for k, v in request.query.items() if k.lower() != "api_key"}

    body = await request.read()
    semaphore = get_island_semaphore(island_param)

    try:
        async with semaphore, session.request(
            method=method,
            url=target_url,
            params=params,
            data=body or None,
            headers=forward_headers,
            timeout=STREAM_TIMEOUT,
            allow_redirects=False,
        ) as upstream_resp:
            out_headers = {
                h: val
                for h, val in upstream_resp.headers.items()
                if h.lower() not in STRIP_RESP
            }

            # 6. Stream response back to client chunk by chunk
            resp = web.StreamResponse(status=upstream_resp.status, headers=out_headers)
            await resp.prepare(request)
            try:
                async for chunk in upstream_resp.content.iter_chunked(16 * 1024):
                    await resp.write(chunk)
                await resp.write_eof()
            except (ConnectionResetError, aiohttp.ClientError, asyncio.CancelledError) as exc:
                logger.debug("Mid-stream connection closed for %s: %s", island_param, exc)
            return resp

    except (aiohttp.ClientConnectorError, OSError):
        return web.json_response(
            {
                "success": False,
                "unreachable": True,
                "error": f"SysBot for '{island_param}' is offline (port {port} not listening).",
                "island_name": island_param,
                "port": port,
            },
            status=503,
        )
    except asyncio.TimeoutError:
        return web.json_response(
            {
                "success": False,
                "unreachable": True,
                "error": f"SysBot for '{island_param}' on port {port} timed out after 30s.",
                "island_name": island_param,
                "port": port,
            },
            status=504,
        )
    except Exception as exc:
        err_msg = str(exc) or type(exc).__name__
        logger.error("Error proxying %s to %s: %s", method, target_url, err_msg)
        return web.json_response(
            {"success": False, "error": f"Bridge proxy error: {err_msg}"},
            status=500,
        )


# ── Application Lifecycle ──────────────────────────────────────────────────────

async def on_startup(app: web.Application) -> None:
    connector = aiohttp.TCPConnector(limit=200, limit_per_host=20, ttl_dns_cache=300)
    # Default timeout of 2.5s is set on the session for cluster fan-out queries
    app[CLIENT_SESSION_KEY] = aiohttp.ClientSession(
        connector=connector,
        timeout=aiohttp.ClientTimeout(total=2.5, connect=1.0),
    )
    logger.info("Shared HTTP client connector initialized (default timeout 2.5s).")


async def on_cleanup(app: web.Application) -> None:
    session: aiohttp.ClientSession = app.get(CLIENT_SESSION_KEY)
    if session and not session.closed:
        await session.close()
    logger.info("Shared HTTP client connector closed.")


def create_app() -> web.Application:
    middlewares = [auth_middleware]
    if CORS_ORIGIN:
        middlewares.insert(0, cors_middleware)

    # Explicit client_max_size (32MB) to support larger payloads such as .nhi dumps or map data
    app = web.Application(client_max_size=32 * 1024 * 1024, middlewares=middlewares)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    # Core Bridge Routes
    app.router.add_get("/", handle_root)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/islands", handle_islands_list)
    app.router.add_get("/cluster/status", handle_cluster_status)
    app.router.add_get("/cluster/telemetry", handle_cluster_telemetry)

    # Transparent proxy routes (supports /islands/<name>/... and /<name>/...)
    app.router.add_route("*", "/islands/{island}/{subpath:.*}", proxy_island_request)
    app.router.add_route("*", "/islands/{island}", proxy_island_request)
    app.router.add_route("*", "/{island}/{subpath:.*}", proxy_island_request)
    app.router.add_route("*", "/{island}", proxy_island_request)

    return app


if __name__ == "__main__":
    # Fail-closed: refuse to start without an API key unless INSECURE_MODE is explicitly enabled
    if not SYSBOT_API_KEY and not INSECURE_MODE:
        logger.critical(
            "FATAL: SYSBOT_API_KEY is not configured in environment or .env!\n"
            "Refusing to start in secure mode. Set SYSBOT_API_KEY or pass --insecure to bypass."
        )
        sys.exit(1)

    ports = list(get_all_island_ports().values())
    min_port = min(ports) if ports else 0
    max_port = max(ports) if ports else 0

    logger.info("=======================================================")
    logger.info("   SysBot Bridge & Cloudflare Tunnel Gateway Server    ")
    logger.info("=======================================================")
    logger.info("Listening on: http://%s:%d", BRIDGE_HOST, BRIDGE_PORT)
    logger.info("Targeting: %d SysBot ports (%d..%d)", len(ALL_ISLANDS), min_port, max_port)
    if INSECURE_MODE:
        logger.warning("=======================================================")
        logger.warning(" [!] WARNING: RUNNING IN INSECURE MODE")
        logger.warning(" Authentication is completely DISABLED.")
        logger.warning(" Any caller can proxy commands to the 48 Switch bots!")
        logger.warning("=======================================================")
    elif SYSBOT_API_KEY:
        logger.info("Authentication: ENABLED (SYSBOT_API_KEY active)")
    if CF_ACCESS_CLIENT_ID and CF_ACCESS_CLIENT_SECRET:
        logger.info("Cloudflare Access Tokens: ENABLED")
    logger.info("=======================================================")

    app = create_app()
    # Safe access_log_format that excludes query parameters to prevent token leaks
    web.run_app(
        app,
        host=BRIDGE_HOST,
        port=BRIDGE_PORT,
        access_log_format='%a %t "%r" %s %b %Tf',
    )
