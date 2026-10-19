"""
SysBot API Client & Bundles Integration Module
Handles REST API interactions with SysBot.ACNHOrders, bundles API, and SQLite order queue sync.
"""

import asyncio
import json
import logging
import os
import re
import time
import threading
from typing import Any, Dict, List, Optional, Tuple, Union

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import urllib3.exceptions

from utils.config import Config
from utils.database import connect_db

logger = logging.getLogger("SysBotAPI")

BUNDLES_API_URL = "https://console.chopaeng.com/api/bundles"
_INVALID_DODO_CODES = frozenset({"00000", "-----", "null", "None", ""})

_SYSBOT_STATUS_MAP = {
    "queued": "queued",
    "next": "preparing",
    "ready": "ready",
    "completed": "completed",
    "cancelled": "cancelled",
    "not_found": "error",
    "error": "error",
    "active": "preparing",
    "in_progress": "preparing",
    "preparing": "preparing",
}


def _ensure_order_table():
    """Ensure the database schema and order_bot_queue table exist."""
    try:
        connect_db().close()
    except Exception as exc:
        logger.warning(f"[SysBotAPI] Failed to ensure database schema: {exc}")
 

def _parse_eta_minutes(data: dict) -> int:
    """Extract estimated minutes from estimated_seconds or eta string."""
    if data.get("estimated_seconds") is not None:
        try:
            return max(1, int(round(float(data["estimated_seconds"]) / 60.0)))
        except (ValueError, TypeError):
            pass
    if data.get("eta"):
        m = re.search(r"(\d+)m", str(data["eta"]))
        if m:
            return max(1, int(m.group(1)))
    return 2


def normalize_order_data(data: dict) -> dict:
    """Normalize order response from SysBot."""
    if not isinstance(data, dict):
        return data

    dodo = data.get("dodo_code")
    has_dodo = bool(dodo and str(dodo).strip() not in _INVALID_DODO_CODES)
    if not has_dodo:
        data["dodo_code"] = None

    raw_status = str(data.get("status") or "queued").lower()
    if has_dodo and raw_status not in ("completed", "cancelled", "error"):
        data["status"] = "ready"
    else:
        data["status"] = _SYSBOT_STATUS_MAP.get(raw_status, raw_status)

    raw_pos = data.get("queue_position") or data.get("position")
    if raw_pos is not None:
        try:
            pos_int = int(raw_pos)
            if pos_int <= 0 and data["status"] not in ("ready", "preparing"):
                pos_int = 1
            data["queue_position"] = max(0, pos_int)
        except (ValueError, TypeError):
            data["queue_position"] = 1
    else:
        data["queue_position"] = 0 if data["status"] in ("ready", "preparing") else 1

    if "estimated_minutes" not in data or data.get("estimated_minutes") is None:
        data["estimated_minutes"] = 0 if data["status"] == "ready" else _parse_eta_minutes(data)

    return data


def parse_order_input(raw_input: str) -> Tuple[str, Optional[str]]:
    """
    Parse a raw order string into (items_string, villager_name_or_id).
    Supports:
      - Name lists: "Gold nugget 30, Iron nugget 30, villager:Raymond"
      - Pure hex sequences: "14BB 16DB 0000002000003604 villager:Raymond"
      - Villager prefix/suffix: "villager:Raymond, Royal crown 40"
    """
    cleaned = re.sub(r"^!(order|drop)\s*", "", raw_input.strip(), flags=re.IGNORECASE).strip()
    if not cleaned:
        return "", None

    villager = None
    # 1. Check for 'villager:<name_or_id>' format
    v_match = re.search(r"\bvillager:([a-zA-Z0-9_\-]+)\b", cleaned, flags=re.IGNORECASE)
    if v_match:
        villager = v_match.group(1).strip()
        # Remove the villager:xxx portion from items
        cleaned = re.sub(r"\bvillager:[a-zA-Z0-9_\-]+\b", "", cleaned, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r",\s*,", ",", cleaned).strip(" ,")

    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,")
    return cleaned, villager


def sanitize_single_drop_item(token: str) -> List[str]:
    """Sanitize a single item token for SysBot/NHSE.
    Ensures non-stackable items (stack_size <= 1) or items with quantity 1
    do not have a trailing quantity appended (e.g. 'tea set 1' -> 'tea set'),
    which causes NHSE ItemParser to crash with 'Failed to convert item for Language en'.
    For non-stackable items with qty > 1, expands them to repeated items (e.g. ['tea set', 'tea set']).
    """
    token = token.strip()
    if not token:
        return []
    # If it is a hex code or raw code (4 to 16 hex digits)
    if re.fullmatch(r"(?:0x)?[0-9a-fA-F]{4,16}", token):
        return [token]

    try:
        from utils.acnh_catalog import ACNHCatalog
        cat = ACNHCatalog.get_instance()
    except Exception:
        cat = None

    if cat:
        # Check if entire token is an exact item name (e.g. 'Marine Song 2001', 'Nintendo Switch 2')
        exact_it = cat.get_item(token)
        if exact_it and exact_it.name.lower() == token.lower():
            return [exact_it.name]

        # Match trailing count, e.g. 'tea set 1', 'Gold nugget 30', 'Royal crown x2'
        m = re.match(r"^(.*?)(?:\s+[xX×*]?\s*(\d+))$", token)
        if m:
            base_name = m.group(1).strip()
            qty = int(m.group(2))
            base_it = cat.get_item(base_name)
            if base_it:
                stack_size = int(getattr(base_it, "stack_size", 1) or 1)
                item_name = base_name if base_name.lower() == base_it.name.lower() else base_it.name
                if stack_size > 1 and qty > 1:
                    return [f"{item_name} {qty}"]
                elif qty > 1:
                    return [item_name] * qty
                else:
                    return [item_name]
            else:
                # Item not in catalog, but ends in 1: strip 1 as NHSE does not use ' 1'
                if qty == 1:
                    return [base_name]

    return [token]


def sanitize_drop_items(items: Union[str, List[str]]) -> str:
    """Sanitize drop items list or string to space-delimited hex codes with NO commas."""
    try:
        from utils.data_manager import resolve_drop_items
        hex_str, _ = resolve_drop_items(items)
        if hex_str:
            return hex_str
    except Exception:
        pass

    if isinstance(items, list):
        res: List[str] = []
        for it in items:
            res.extend(sanitize_single_drop_item(str(it)))
        return " ".join(res)
    elif isinstance(items, str):
        parts = [p.strip() for p in re.split(r"[,;\n\r]+", items) if p.strip()]
        res = []
        for p in parts:
            res.extend(sanitize_single_drop_item(p))
        return " ".join(res)
    return str(items)


class SysBotClient:
    """HTTP Client for communicating with SysBot.ACNHOrders REST API and external bundles."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: int = 10,
    ):
        raw_url = base_url if base_url is not None else getattr(Config, "SYSBOT_API_URL", "")
        self.base_url = (raw_url or "").rstrip("/")
        self.api_key = api_key if api_key is not None else getattr(Config, "SYSBOT_API_KEY", "") or ""
        self.timeout = timeout
        self._session: Optional[requests.Session] = None
        self._bundles_cache: Optional[List[Dict[str, Any]]] = None
        self._bundles_cache_time: float = 0
        self._bundles_cache_ttl: float = 300  # 5 minutes
        self._offline_until: float = 0.0
        self._offline_cooldown: float = 10.0

        _ensure_order_table()

    def _get_session(self) -> requests.Session:
        if self._session is None:
            self._session = requests.Session()
            retry = Retry(
                total=1,
                connect=False,  # Never retry when target actively refused connection / offline
                read=1,
                status=1,
                backoff_factor=0.3,
                status_forcelist=[502, 503, 504],
                allowed_methods=["GET", "POST", "DELETE"],
                raise_on_status=False,
            )
            adapter = HTTPAdapter(max_retries=retry, pool_connections=5, pool_maxsize=10)
            self._session.mount("https://", adapter)
            self._session.mount("http://", adapter)
        return self._session

    def _headers(self) -> dict:
        h = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.api_key:
            h["X-API-Key"] = self.api_key
        return h

    # ── Synchronous HTTP Request Helpers ─────────────────────────────────────

    def _get(self, path: str, **params) -> Tuple[dict, int]:
        if not self.base_url:
            return {"success": False, "error": "SysBot API is not configured."}, 503

        now = time.monotonic()
        if path in ("/api/status", "/api/queue", "/api/villagers", "/api/events") and now < self._offline_until:
            return {"success": False, "error": "SysBot API is unreachable or offline."}, 503

        url = f"{self.base_url}{path}"
        timeout_val = (1.5, 3.0) if path in ("/api/status", "/api/queue", "/api/villagers", "/api/events") else (3.0, float(self.timeout))
        try:
            resp = self._get_session().get(
                url,
                headers=self._headers(),
                params={k: v for k, v in params.items() if v is not None},
                timeout=timeout_val,
            )
            if resp.status_code in (502, 503, 504):
                self._offline_until = time.monotonic() + self._offline_cooldown
            else:
                self._offline_until = 0.0
            try:
                parsed = resp.json()
                if isinstance(parsed, list):
                    return ({"success": resp.ok, "villagers": parsed} if path == "/api/villagers" else {"success": resp.ok, "data": parsed}), resp.status_code
                return parsed, resp.status_code
            except Exception:
                return {"success": False, "error": f"HTTP {resp.status_code}: non-JSON response"}, resp.status_code
        except (requests.exceptions.ConnectionError, urllib3.exceptions.HTTPError, OSError):
            self._offline_until = time.monotonic() + self._offline_cooldown
            return {"success": False, "error": "SysBot API is unreachable or offline."}, 503
        except requests.exceptions.Timeout:
            self._offline_until = time.monotonic() + self._offline_cooldown
            return {"success": False, "error": "SysBot API request timed out."}, 504
        except Exception as exc:
            self._offline_until = time.monotonic() + self._offline_cooldown
            return {"success": False, "error": str(exc)}, 500

    def _post(self, path: str, payload: Any) -> Tuple[dict, int]:
        if not self.base_url:
            return {"success": False, "error": "SysBot API is not configured."}, 503

        now = time.monotonic()
        if now < self._offline_until and path != "/api/order/cancel":
            return {"success": False, "error": "SysBot API is unreachable or offline."}, 503

        url = f"{self.base_url}{path}"
        try:
            resp = self._get_session().post(
                url,
                headers=self._headers(),
                json=payload if payload is not None else {},
                timeout=(3.0, float(self.timeout)),
            )
            if resp.status_code in (502, 503, 504):
                self._offline_until = time.monotonic() + self._offline_cooldown
            else:
                self._offline_until = 0.0
            try:
                data = resp.json()
                if isinstance(data, dict):
                    return data, resp.status_code
                return {"success": resp.status_code < 400, "data": data}, resp.status_code
            except Exception:
                err_text = resp.text.strip() if resp.text else f"HTTP {resp.status_code}: non-JSON response"
                clean_err = re.sub(r"^HttpAPI:\s*(?:Error in API handler:\s*)?", "", err_text, flags=re.IGNORECASE).strip()
                first_line = clean_err.split("\n")[0].strip() if clean_err else f"HTTP {resp.status_code}"
                return {"success": False, "error": first_line}, resp.status_code
        except (requests.exceptions.ConnectionError, urllib3.exceptions.HTTPError, OSError):
            self._offline_until = time.monotonic() + self._offline_cooldown
            return {"success": False, "error": "SysBot API is unreachable or offline."}, 503
        except requests.exceptions.Timeout:
            self._offline_until = time.monotonic() + self._offline_cooldown
            return {"success": False, "error": "SysBot API request timed out."}, 504
        except Exception as exc:
            self._offline_until = time.monotonic() + self._offline_cooldown
            return {"success": False, "error": str(exc)}, 500

    def _delete(self, path: str, **params) -> Tuple[dict, int]:
        if not self.base_url:
            return {"success": False, "error": "SysBot API is not configured."}, 503

        now = time.monotonic()
        if now < self._offline_until:
            return {"success": False, "error": "SysBot API is unreachable or offline."}, 503

        url = f"{self.base_url}{path}"
        try:
            resp = self._get_session().delete(
                url,
                headers=self._headers(),
                params={k: v for k, v in params.items() if v is not None},
                timeout=(3.0, float(self.timeout)),
            )
            if resp.status_code in (502, 503, 504):
                self._offline_until = time.monotonic() + self._offline_cooldown
            else:
                self._offline_until = 0.0
            try:
                return resp.json(), resp.status_code
            except Exception:
                return {"success": False, "error": f"HTTP {resp.status_code}: non-JSON response"}, resp.status_code
        except (requests.exceptions.ConnectionError, urllib3.exceptions.HTTPError, OSError):
            self._offline_until = time.monotonic() + self._offline_cooldown
            return {"success": False, "error": "SysBot API is unreachable or offline."}, 503
        except requests.exceptions.Timeout:
            self._offline_until = time.monotonic() + self._offline_cooldown
            return {"success": False, "error": "SysBot API request timed out."}, 504
        except Exception as exc:
            self._offline_until = time.monotonic() + self._offline_cooldown
            return {"success": False, "error": str(exc)}, 500

    # ── Async API Methods ───────────────────────────────────────────────────

    async def get_bot_status(self) -> dict:
        """Fetch SysBot island and order bot state."""
        island_default = getattr(Config, "ORDER_BOT_ISLAND", "Sinta")
        if not self.base_url or time.monotonic() < self._offline_until:
            return {
                "success": False,
                "error": "SysBot API is not configured." if not self.base_url else "SysBot API is offline.",
                "island_name": island_default,
                "is_running": False,
                "accepting_commands": False,
                "is_connected": False,
                "console_stage": "Offline",
                "is_dodo_valid": False,
                "is_refreshing_dodo": False,
                "queue_count": 0,
            }

        data, code = await asyncio.to_thread(self._get, "/api/status")
        if not isinstance(data, dict):
            data = {"success": False, "error": "Invalid response"}

        if code == 200 or data.get("success"):
            data.setdefault("island_name", island_default)
            data.setdefault("is_running", True)
            data.setdefault("accepting_commands", True)
            data.setdefault("is_connected", True)
            data.setdefault("console_stage", "Running")
            dodo = data.get("dodo_code")
            is_valid_dodo = bool(dodo and str(dodo).strip() not in _INVALID_DODO_CODES)
            data.setdefault("is_dodo_valid", is_valid_dodo)
            data.setdefault("is_refreshing_dodo", not is_valid_dodo)
            data.setdefault("queue_count", 0)
        else:
            data.setdefault("is_running", False)
            data.setdefault("accepting_commands", False)
            data.setdefault("is_connected", False)
            data.setdefault("console_stage", "Stopped")
            data.setdefault("is_dodo_valid", False)
            data.setdefault("is_refreshing_dodo", False)
            data.setdefault("island_name", island_default)
            data.setdefault("queue_count", 0)

        return data

    async def get_game_time(self) -> dict:
        """Fetch in-game time and Nook's Cranny status from GET /api/gametime."""
        if not self.base_url or time.monotonic() < self._offline_until:
            return {"success": False, "error": "SysBot API is offline or not configured."}

        data, code = await asyncio.to_thread(self._get, "/api/gametime")
        if not isinstance(data, dict):
            return {"success": False, "error": "Invalid response"}
        return data

    def get_game_time_sync(self) -> dict:
        """Synchronously fetch in-game time and Nook's Cranny status from GET /api/gametime."""
        if not self.base_url or time.monotonic() < self._offline_until:
            return {"success": False, "error": "SysBot API is offline or not configured."}

        data, code = self._get("/api/gametime")
        if not isinstance(data, dict):
            return {"success": False, "error": "Invalid response"}
        return data

    async def submit_order(
        self,
        username: str,
        order_text: str = "",
        items: Optional[List[str]] = None,
        villager: Optional[str] = None,
        user_id: Optional[str] = None,
        order_id: Optional[str] = None,
    ) -> dict:
        """
        Submit an order to SysBot and persist to SQLite DB.
        Returns normalized order submission result.
        """
        parsed_order, parsed_villager = parse_order_input(order_text)
        final_order = parsed_order or ""
        final_villager = villager or parsed_villager

        payload: Dict[str, Any] = {"username": username}
        if final_order:
            payload["order"] = final_order
        elif items:
            payload["items"] = items

        if final_villager:
            payload["villager"] = final_villager
        if user_id:
            payload["user_id"] = str(user_id)
        if order_id:
            payload["order_id"] = str(order_id)

        result, code = await asyncio.to_thread(self._post, "/api/order", payload)
        result = normalize_order_data(result)

        # Persist order to database if successful
        if isinstance(result, dict) and (result.get("success") or code in (200, 201)):
            assigned_id = str(result.get("order_id") or result.get("id") or order_id or f"ord_{int(time.time())}")
            result["order_id"] = assigned_id
            now_ts = int(time.time())
            cmd_str = final_order or (", ".join(items) if items else (f"villager:{final_villager}" if final_villager else "Order"))
            if final_villager and f"villager:{final_villager}" not in cmd_str:
                cmd_str = f"{cmd_str}, villager:{final_villager}"

            try:
                with connect_db() as conn:
                    conn.execute(
                        """
                        REPLACE INTO order_bot_queue
                            (id, user_id, username, command, order_type, status,
                             queue_position, estimated_minutes, dodo_code, island_name,
                             message, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            assigned_id,
                            str(user_id or "anonymous"),
                            username,
                            cmd_str,
                            "order",
                            result.get("status", "queued"),
                            int(result.get("queue_position") or 1),
                            int(result.get("estimated_minutes") or 2),
                            result.get("dodo_code"),
                            result.get("island_name") or getattr(Config, "ORDER_BOT_ISLAND", "Sinta"),
                            result.get("message") or "Order placed via Order Panel",
                            now_ts,
                            now_ts,
                        ),
                    )
                    conn.commit()
            except Exception as exc:
                logger.warning(f"[SysBotAPI] Failed to save order {assigned_id} to DB: {exc}")

        return result

    async def get_order_status(self, order_id: str) -> dict:
        """Poll order status from SysBot and update DB."""
        if not order_id:
            return {"success": False, "error": "Order ID is required."}

        data, code = await asyncio.to_thread(self._get, "/api/order/status", id=order_id)
        data = normalize_order_data(data)

        if isinstance(data, dict) and data.get("status"):
            now_ts = int(time.time())
            try:
                with connect_db() as conn:
                    conn.execute(
                        """
                        UPDATE order_bot_queue
                        SET status = ?,
                            queue_position = COALESCE(?, queue_position),
                            estimated_minutes = COALESCE(?, estimated_minutes),
                            dodo_code = COALESCE(?, dodo_code),
                            island_name = COALESCE(?, island_name),
                            message = COALESCE(?, message),
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            data.get("status"),
                            data.get("queue_position"),
                            data.get("estimated_minutes"),
                            data.get("dodo_code"),
                            data.get("island_name"),
                            data.get("message"),
                            now_ts,
                            order_id,
                        ),
                    )
                    conn.commit()
            except Exception as exc:
                logger.warning(f"[SysBotAPI] Failed to update order status for {order_id}: {exc}")

        return data

    async def cancel_order(self, order_id: str) -> dict:
        """Cancel an active order on SysBot and mark cancelled in DB."""
        if not order_id:
            return {"success": False, "error": "Order ID is required."}

        result, code = await asyncio.to_thread(self._post, "/api/order/cancel", {"id": order_id})
        try:
            with connect_db() as conn:
                conn.execute(
                    "UPDATE order_bot_queue SET status = 'cancelled', updated_at = ? WHERE id = ?",
                    (int(time.time()), order_id),
                )
                conn.commit()
        except Exception as exc:
            logger.warning(f"[SysBotAPI] Failed to mark order {order_id} as cancelled: {exc}")

        return result

    async def get_queue(self) -> dict:
        """Fetch the live queue of pending orders."""
        data, code = await asyncio.to_thread(self._get, "/api/queue")
        if isinstance(data, dict) and isinstance(data.get("orders"), list):
            for entry in data["orders"]:
                if "position" in entry and "queue_position" not in entry:
                    entry["queue_position"] = entry["position"]
                raw_st = str(entry.get("status") or "queued").lower()
                entry["status"] = _SYSBOT_STATUS_MAP.get(raw_st, raw_st)
                if "estimated_minutes" not in entry:
                    entry["estimated_minutes"] = _parse_eta_minutes(entry)
            data["queue"] = data["orders"]
        elif isinstance(data, dict) and not data.get("orders"):
            data["orders"] = []
            data["queue"] = []
        return data

    async def get_dodo(self, order_id: Optional[str] = None, user_id: Optional[str] = None) -> dict:
        """Fetch Dodo code for an order or drop mode."""
        params = {}
        if order_id:
            params["order_id"] = order_id
        if user_id:
            params["user_id"] = str(user_id)
        data, code = await asyncio.to_thread(self._get, "/api/dodo", **params)
        return data

    async def get_presets(self) -> List[str]:
        """Fetch preset names configured directly in SysBot."""
        data, code = await asyncio.to_thread(self._get, "/api/presets")
        if isinstance(data, dict) and isinstance(data.get("presets"), list):
            return data["presets"]
        if isinstance(data, list):
            return data
        return []

    async def get_bundles(self) -> List[Dict[str, Any]]:
        """Fetch official bundles from https://console.chopaeng.com/api/bundles."""
        now = time.time()
        if self._bundles_cache and (now - self._bundles_cache_time) < self._bundles_cache_ttl:
            return self._bundles_cache

        def _fetch():
            try:
                resp = requests.get(BUNDLES_API_URL, timeout=8)
                if resp.status_code == 200:
                    return resp.json()
            except Exception as e:
                logger.warning(f"[SysBotAPI] Failed to fetch bundles from {BUNDLES_API_URL}: {e}")
            return None

        data = await asyncio.to_thread(_fetch)
        if isinstance(data, list) and data:
            self._bundles_cache = data
            self._bundles_cache_time = now
            return data

        # Fallback default presets if external API is unreachable
        return [
            {
                "id": "bundle-materials",
                "name": "Essential Materials",
                "title": "Essential Materials",
                "category": "Popular",
                "description": "Full stack of essential crafting materials (Gold, Iron, Wood, NMT).",
                "orderItems": [
                    {"itemId": "09C9", "name": "Gold nugget", "quantity": 30, "category": "Materials", "image": "https://dodo.ac/np/images/2/26/Gold_Nugget_NH_Inv_Icon.png"},
                    {"itemId": "09CF", "name": "Iron nugget", "quantity": 30, "category": "Materials", "image": "https://dodo.ac/np/images/5/52/Iron_Nugget_NH_Inv_Icon.png"},
                    {"itemId": "16DB", "name": "Nook Miles Ticket", "quantity": 10, "category": "Currency", "image": "https://dodo.ac/np/images/4/43/Nook_Miles_Ticket_NH_Inv_Icon.png"},
                    {"itemId": "0AD0", "name": "Wood", "quantity": 30, "category": "Materials", "image": "https://dodo.ac/np/images/d/df/Wood_NH_Inv_Icon.png"},
                ],
            },
            {
                "id": "bundle-wealth",
                "name": "Crowns & Wealth Pack",
                "title": "Crowns & Wealth Pack",
                "category": "Wealth",
                "description": "Max value Bell bundles, Royal Crowns, and Nook Miles Tickets.",
                "orderItems": [
                    {"itemId": "14BB", "name": "Royal Crown", "quantity": 10, "category": "Clothing", "image": "https://dodo.ac/np/images/c/c7/Royal_Crown_NH_Storage_Icon.png"},
                    {"itemId": "16DB", "name": "Nook Miles Ticket", "quantity": 10, "category": "Currency", "image": "https://dodo.ac/np/images/4/43/Nook_Miles_Ticket_NH_Inv_Icon.png"},
                    {"itemId": "09C9", "name": "Gold nugget", "quantity": 30, "category": "Materials", "image": "https://dodo.ac/np/images/2/26/Gold_Nugget_NH_Inv_Icon.png"},
                ],
            },
            {
                "id": "bundle-golden-tools",
                "name": "Golden Tools Set",
                "title": "Golden Tools Set",
                "category": "Tools",
                "description": "Complete set of durable Golden Tools for island maintenance.",
                "orderItems": [
                    {"itemId": "2591", "name": "Golden axe", "quantity": 1, "category": "Tools/Goods", "image": "https://dodo.ac/np/images/8/87/Golden_Axe_NH_Inv_Icon.png"},
                    {"itemId": "217E", "name": "Golden shovel", "quantity": 1, "category": "Tools/Goods", "image": "https://dodo.ac/np/images/e/e5/Golden_Shovel_NH_Inv_Icon.png"},
                    {"itemId": "1FF3", "name": "Golden net", "quantity": 1, "category": "Tools/Goods", "image": "https://dodo.ac/np/images/7/7b/Golden_Net_NH_Inv_Icon.png"},
                    {"itemId": "2155", "name": "Golden watering can", "quantity": 1, "category": "Tools/Goods", "image": "https://dodo.ac/np/images/2/2c/Golden_Watering_Can_NH_Inv_Icon.png"},
                    {"itemId": "2182", "name": "Golden slingshot", "quantity": 1, "category": "Tools/Goods", "image": "https://dodo.ac/np/images/6/6f/Golden_Slingshot_NH_Inv_Icon.png"},
                ],
            },
            {
                "id": "bundle-celestial",
                "name": "Star Fragments & Celestial",
                "title": "Star Fragments & Celestial",
                "category": "Seasonal",
                "description": "Celeste crafting essentials including regular and large Star Fragments.",
                "orderItems": [
                    {"itemId": "175F", "name": "Star fragment", "quantity": 30, "category": "Materials", "image": "https://dodo.ac/np/images/6/64/Star_Fragment_NH_Inv_Icon.png"},
                    {"itemId": "1760", "name": "Large star fragment", "quantity": 10, "category": "Materials", "image": "https://dodo.ac/np/images/5/5a/Large_Star_Fragment_NH_Inv_Icon.png"},
                ],
            },
        ]

    async def get_user_order_history(self, user_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Fetch user order history from the SQLite database."""
        if not user_id:
            return []

        def _query():
            orders = []
            try:
                from utils.unified_auth import resolve_all_user_ids
                cands = resolve_all_user_ids(str(user_id))
            except Exception:
                cands = [str(user_id)]

            if not cands:
                cands = [str(user_id)]

            placeholders = ",".join("?" for _ in cands)
            try:
                with connect_db() as conn:
                    cur = conn.execute(
                        f"""
                        SELECT id, user_id, username, command, order_type, status,
                               queue_position, estimated_minutes, dodo_code, island_name,
                               message, created_at, updated_at
                        FROM order_bot_queue
                        WHERE user_id IN ({placeholders})
                        ORDER BY created_at DESC
                        LIMIT ?
                        """,
                        tuple(cands) + (limit,),
                    )
                    rows = cur.fetchall()
                    for r in rows:
                        orders.append({
                            "id": r["id"],
                            "user_id": r["user_id"],
                            "username": r["username"] or "",
                            "command": r["command"] or "",
                            "order_type": r["order_type"] or "order",
                            "status": r["status"] or "queued",
                            "queue_position": r["queue_position"],
                            "estimated_minutes": r["estimated_minutes"],
                            "dodo_code": r["dodo_code"],
                            "island_name": r["island_name"] or "Sinta",
                            "message": r["message"] or "",
                            "created_at": r["created_at"],
                            "updated_at": r["updated_at"],
                        })
            except Exception as exc:
                logger.warning(f"[SysBotAPI] Failed to fetch user order history: {exc}")
            return orders

        return await asyncio.to_thread(_query)

    def get_bot_status_sync(self) -> dict:
        """Fetch SysBot island and order bot state synchronously."""
        island_default = getattr(Config, "ORDER_BOT_ISLAND", "Sinta")
        if not self.base_url or time.monotonic() < self._offline_until:
            return {
                "success": False,
                "error": "SysBot API is not configured." if not self.base_url else "SysBot API is offline.",
                "island_name": island_default,
                "is_running": False,
                "accepting_commands": False,
                "is_connected": False,
                "console_stage": "Offline",
                "is_dodo_valid": False,
                "is_refreshing_dodo": False,
                "queue_count": 0,
            }

        data, code = self._get("/api/status")
        if not isinstance(data, dict):
            data = {"success": False, "error": "Invalid response"}

        if code == 200 or data.get("success"):
            data.setdefault("island_name", island_default)
            data.setdefault("is_running", True)
            data.setdefault("accepting_commands", True)
            data.setdefault("is_connected", True)
            data.setdefault("console_stage", "Running")
            dodo = data.get("dodo_code")
            is_valid_dodo = bool(dodo and str(dodo).strip() not in _INVALID_DODO_CODES)
            data.setdefault("is_dodo_valid", is_valid_dodo)
            data.setdefault("is_refreshing_dodo", not is_valid_dodo)
            data.setdefault("queue_count", 0)
        else:
            data.setdefault("is_running", False)
            data.setdefault("accepting_commands", False)
            data.setdefault("is_connected", False)
            data.setdefault("console_stage", "Stopped")
            data.setdefault("is_dodo_valid", False)
            data.setdefault("is_refreshing_dodo", False)
            data.setdefault("island_name", island_default)
            data.setdefault("queue_count", 0)

        return data

    def get_villagers_sync(self) -> dict:
        """Fetch current island villagers from SysBot GET /api/villagers."""
        data, code = self._get("/api/villagers")
        if not isinstance(data, dict):
            return {"success": False, "error": f"Invalid response from SysBot (HTTP {code}).", "villagers": []}
        return data

    async def get_villagers(self) -> dict:
        """Fetch the list of villagers currently residing on the island from SysBot."""
        return await asyncio.to_thread(self.get_villagers_sync)

    def inject_villagers_sync(
        self,
        villagers: Union[str, Dict[str, Any], List[Union[str, Dict[str, Any]]]],
        plot: Optional[int] = None,
    ) -> dict:
        """Inject one or more villagers into island house plots via SysBot POST /api/villagers."""
        if not villagers:
            return {"success": False, "error": "No villager specified for injection."}

        try:
            from utils.data_manager import resolve_villager_code
        except Exception:
            resolve_villager_code = None

        payload: Any
        if isinstance(villagers, str):
            v_str = villagers.strip()
            if v_str.startswith("!inject"):
                parts = v_str.split(None, 2)
                target_plot = None
                target_v = ""
                if len(parts) >= 3 and parts[1].isdigit():
                    target_plot = int(parts[1])
                    target_v = parts[2]
                elif len(parts) >= 2:
                    target_v = parts[1]
                resolved_v = resolve_villager_code(target_v) if (resolve_villager_code and target_v) else (target_v or v_str)
                p = target_plot if target_plot is not None else plot
                if p is not None:
                    payload = {"villager": resolved_v, "plot": int(p)}
                else:
                    payload = {"villager": resolved_v}
            else:
                resolved_v = resolve_villager_code(v_str) if resolve_villager_code else v_str
                if plot is not None:
                    payload = {"villager": resolved_v, "plot": int(plot)}
                else:
                    payload = {"villager": resolved_v}
        elif isinstance(villagers, dict):
            payload = dict(villagers)
            if "villager" in payload and resolve_villager_code:
                payload["villager"] = resolve_villager_code(str(payload["villager"]))
            elif "villager_name" in payload and resolve_villager_code:
                payload["villager_name"] = resolve_villager_code(str(payload["villager_name"]))
            if plot is not None and "plot" not in payload:
                payload["plot"] = int(plot)
        elif isinstance(villagers, list):
            payload = []
            for item in villagers:
                if isinstance(item, dict):
                    item_copy = dict(item)
                    if "villager" in item_copy and resolve_villager_code:
                        item_copy["villager"] = resolve_villager_code(str(item_copy["villager"]))
                    elif "villager_name" in item_copy and resolve_villager_code:
                        item_copy["villager_name"] = resolve_villager_code(str(item_copy["villager_name"]))
                    payload.append(item_copy)
                elif isinstance(item, str) and resolve_villager_code:
                    payload.append({"villager": resolve_villager_code(item)})
                else:
                    payload.append(item)
        else:
            payload = str(villagers)

        data, code = self._post("/api/villagers", payload)
        if not isinstance(data, dict):
            return {"success": False, "error": f"Invalid response from SysBot (HTTP {code})."}
        return data

    async def inject_villagers(
        self,
        villagers: Union[str, Dict[str, Any], List[Union[str, Dict[str, Any]]]],
        plot: Optional[int] = None,
    ) -> dict:
        """Inject one or more villagers into island house plots via SysBot POST /api/villagers."""
        return await asyncio.to_thread(self.inject_villagers_sync, villagers, plot)

    def get_events_sync(
        self,
        limit: int = 50,
        since_id: Optional[int] = None,
        event_type: Optional[str] = None,
    ) -> dict:
        """Fetch ring-buffer island events (arrivals, departures, dodos) via GET /api/events."""
        params: Dict[str, Any] = {"limit": limit}
        if since_id is not None:
            params["since_id"] = since_id
        if event_type:
            params["type"] = event_type

        data, code = self._get("/api/events", **params)
        if (code == 404 or not isinstance(data, dict) or not data.get("success")) and code != 503:
            data_act, code_act = self._get("/api/activity", **params)
            if isinstance(data_act, dict) and data_act.get("success"):
                data, code = data_act, code_act
        if not isinstance(data, dict):
            return {"success": False, "error": f"Invalid response from SysBot (HTTP {code}).", "events": []}
        return data

    async def get_events(
        self,
        limit: int = 50,
        since_id: Optional[int] = None,
        event_type: Optional[str] = None,
    ) -> dict:
        """Fetch ring-buffer island events asynchronously."""
        return await asyncio.to_thread(self.get_events_sync, limit, since_id, event_type)

    def refresh_dodo_sync(self, delay_seconds: int = 0) -> dict:
        """Trigger SysBot to reboot game loop and fetch fresh Dodo code via POST /api/dodo/refresh."""
        payload = {"delay_seconds": max(0, int(delay_seconds))}
        data, code = self._post("/api/dodo/refresh", payload)
        if not isinstance(data, dict):
            return {"success": False, "error": f"Invalid response from SysBot (HTTP {code})."}
        return data

    async def refresh_dodo(self, delay_seconds: int = 0) -> dict:
        """Trigger SysBot Dodo refresh asynchronously."""
        return await asyncio.to_thread(self.refresh_dodo_sync, delay_seconds)

    def click_button_sync(self, button: str = "UNSTICK", count: int = 1, delay_ms: int = 200) -> dict:
        """Send virtual controller button press via POST /api/click."""
        payload = {"button": button, "count": count, "delay_ms": delay_ms}
        data, code = self._post("/api/click", payload)
        if not isinstance(data, dict):
            return {"success": False, "error": f"Invalid response from SysBot (HTTP {code})."}
        return data

    async def click_button(self, button: str = "UNSTICK", count: int = 1, delay_ms: int = 200) -> dict:
        """Send virtual controller button press asynchronously."""
        return await asyncio.to_thread(self.click_button_sync, button, count, delay_ms)

    def submit_drop_sync(
        self,
        items: Union[str, List[str]],
        drop_type: str = "items",
        username: str = "WebUser",
        count: Optional[int] = None,
    ) -> dict:
        """Request items or DIY recipes to be dropped on the ground via SysBot POST /api/drop."""
        payload: Dict[str, Any] = {"type": drop_type, "username": username}
        cleaned_items = sanitize_drop_items(items)
        if isinstance(cleaned_items, list):
            payload["items"] = cleaned_items
        else:
            payload["items"] = str(cleaned_items)

        if count is not None:
            payload["count"] = int(count)

        data, code = self._post("/api/drop", payload)
        if not isinstance(data, dict):
            return {"success": False, "error": f"Invalid response from SysBot (HTTP {code})."}
        return data

    async def submit_drop(
        self,
        items: Union[str, List[str]],
        drop_type: str = "items",
        username: str = "WebUser",
        count: Optional[int] = None,
    ) -> dict:
        """Request items or DIY recipes to be dropped asynchronously."""
        return await asyncio.to_thread(self.submit_drop_sync, items, drop_type, username, count)

    def submit_clean_sync(self) -> dict:
        """Clean dropped items synchronously via POST /api/clean."""
        data, code = self._post("/api/clean", {})
        if not isinstance(data, dict):
            return {"success": False, "error": f"Invalid response from SysBot (HTTP {code})."}
        return data

    async def submit_clean(self) -> dict:
        """Order the bot to pick up (clean) all dropped items on the ground via SysBot POST /api/clean."""
        return await asyncio.to_thread(self.submit_clean_sync)

    def set_turnips_sync(self, price: int = 999999999) -> dict:
        """Set island turnip stalk market price synchronously via POST /api/turnips."""
        data, code = self._post("/api/turnips", {"value": int(price)})
        if not isinstance(data, dict):
            return {"success": False, "error": f"Invalid response from SysBot (HTTP {code})."}
        return data

    async def set_turnips(self, price: int = 999999999) -> dict:
        """Set island turnip stalk market price via SysBot POST /api/turnips."""
        return await asyncio.to_thread(self.set_turnips_sync, price)

    def speak_sync(self, message: str) -> dict:
        """Send chat message to Switch in-game chat synchronously via POST /api/speak."""
        data, code = self._post("/api/speak", {"message": str(message)})
        if not isinstance(data, dict):
            return {"success": False, "error": f"Invalid response from SysBot (HTTP {code})."}
        return data

    async def speak(self, message: str) -> dict:
        """Send a chat message to the in-game chat on the Switch via SysBot POST /api/speak."""
        return await asyncio.to_thread(self.speak_sync, message)

    def control_action_sync(self, action: str, **kwargs) -> dict:
        """
        Execute control action via SysBot control API.
        Attempts POST /api/control with {"action": action}, falling back to dedicated REST endpoints.
        Supported actions:
          start, stop, restart, pause, resume, screen-on, screen-off, screen,
          detach, queue, newdodo, mash-on, mash-off, mash, map-on, map-off, map, guide
        """
        action_clean = action.strip().lower()
        # 1. Try unified dispatcher endpoint: POST /api/control
        payload: Dict[str, Any] = {"action": action_clean}
        payload.update(kwargs)
        data, code = self._post("/api/control", payload)
        if code != 404 and isinstance(data, dict) and not (code >= 500 and not data.get("success")):
            return data

        # 2. Fallback to dedicated REST endpoints
        if action_clean in ("start", "startbot"):
            res, _ = self._post("/api/control/start", {})
            return res
        elif action_clean in ("stop", "stopbot"):
            res, _ = self._post("/api/control/stop", {})
            return res
        elif action_clean in ("restart", "restartconnection"):
            res, _ = self._post("/api/control/restart", {})
            return res
        elif action_clean in ("pause", "pause_requests"):
            res, _ = self._post("/api/requests/pause", {})
            return res
        elif action_clean in ("resume", "resume_requests"):
            res, _ = self._post("/api/requests/resume", {})
            return res
        elif action_clean in ("screen-on", "screen_on"):
            res, _ = self._post("/api/screen", {"state": "on"})
            return res
        elif action_clean in ("screen-off", "screen_off"):
            res, _ = self._post("/api/screen", {"state": "off"})
            return res
        elif action_clean in ("screen", "setscreen"):
            state = kwargs.get("state", "toggle")
            res, _ = self._post("/api/screen", {"state": state} if state != "toggle" else {})
            return res
        elif action_clean in ("detach", "controller_detach"):
            res, _ = self._post("/api/detach", {})
            return res
        elif action_clean in ("queue", "getqueue"):
            res, _ = self._get("/api/queue")
            return res
        elif action_clean in ("newdodo", "refreshdodo", "refresh_dodo"):
            res, _ = self._post("/api/dodo/refresh", {})
            return res
        elif action_clean in ("mash-on", "mash_on"):
            res, _ = self._post("/api/togglemashb", {"enabled": True})
            return res
        elif action_clean in ("mash-off", "mash_off"):
            res, _ = self._post("/api/togglemashb", {"enabled": False})
            return res
        elif action_clean in ("mash", "togglemashb"):
            enabled = kwargs.get("enabled")
            payload_m = {"enabled": enabled} if enabled is not None else {}
            res, _ = self._post("/api/togglemashb", payload_m)
            return res
        elif action_clean in ("map-on", "map_on"):
            res, _ = self._post("/api/togglerefresh", {"enabled": True})
            return res
        elif action_clean in ("map-off", "map_off"):
            res, _ = self._post("/api/togglerefresh", {"enabled": False})
            return res
        elif action_clean in ("map", "togglerefresh"):
            enabled = kwargs.get("enabled")
            payload_r = {"enabled": enabled} if enabled is not None else {}
            res, _ = self._post("/api/togglerefresh", payload_r)
            return res
        elif action_clean in ("echo-dodo", "echo_dodo", "echododo"):
            enabled = kwargs.get("enabled")
            return self.toggle_echo_sync("dodo", enabled)
        elif action_clean in ("echo-arrivals", "echo_arrivals", "echoarrivals"):
            enabled = kwargs.get("enabled")
            return self.toggle_echo_sync("arrivals", enabled)
        elif action_clean in ("echo-updates", "echo_updates", "echo_island_updates", "echoupdates"):
            enabled = kwargs.get("enabled")
            return self.toggle_echo_sync("island_updates", enabled)
        elif action_clean in ("echo-all", "echo_all", "echoall", "echo"):
            enabled = kwargs.get("enabled")
            echo_type = kwargs.get("type", "all")
            return self.toggle_echo_sync(echo_type, enabled)
        elif action_clean in ("guide", "control_guide"):
            res, _ = self._get("/api/control/guide")
            return res

        return data if isinstance(data, dict) else {"success": False, "error": f"Unknown control action: {action}"}

    def get_echo_status_sync(self) -> dict:
        """Fetch echo status via GET /api/echo."""
        res, code = self._get("/api/echo")
        if isinstance(res, dict) and code < 400:
            return res
        return {"success": False, "error": f"HTTP {code}"}

    async def get_echo_status(self) -> dict:
        """Fetch echo status asynchronously."""
        return await asyncio.to_thread(self.get_echo_status_sync)


    def toggle_echo_sync(self, echo_type: str = "all", enabled: Optional[bool] = None, **kwargs) -> dict:
        """Toggle or set echo state via POST /api/echo or POST /api/toggleecho."""
        payload: Dict[str, Any] = {}
        target = (echo_type or "all").strip().lower()
        if target == "all":
            payload["type"] = "all"
            if enabled is not None:
                payload["enabled"] = enabled
                payload["echo_dodo_enabled"] = enabled
                payload["echo_arrivals_enabled"] = enabled
                payload["echo_island_updates_enabled"] = enabled
        elif target == "dodo":
            if enabled is not None:
                payload["echo_dodo_enabled"] = enabled
            else:
                payload["type"] = "dodo"
        elif target in ("arrivals", "arrival", "departures", "departure"):
            if enabled is not None:
                payload["echo_arrivals_enabled"] = enabled
            else:
                payload["type"] = "arrivals"
        elif target in ("island_updates", "islandupdates", "updates", "island"):
            if enabled is not None:
                payload["echo_island_updates_enabled"] = enabled
            else:
                payload["type"] = "island_updates"
        else:
            payload["type"] = echo_type
            if enabled is not None:
                payload["enabled"] = enabled

        payload.update(kwargs)
        res, code = self._post("/api/echo", payload)
        if code != 404 and isinstance(res, dict):
            return res
        res, code = self._post("/api/toggleecho", payload)
        return res if isinstance(res, dict) else {"success": False, "error": f"HTTP {code}"}

    async def toggle_echo(self, echo_type: str = "all", enabled: Optional[bool] = None, **kwargs) -> dict:
        """Toggle or set echo state asynchronously."""
        return await asyncio.to_thread(self.toggle_echo_sync, echo_type, enabled, **kwargs)

    async def control_action(self, action: str, **kwargs) -> dict:
        """Execute control action asynchronously."""
        return await asyncio.to_thread(self.control_action_sync, action, **kwargs)


# ============================================================================
# MULTI-ISLAND CLIENT POOL (48 PORTS)
# ============================================================================

class SysBotClientPool:
    """Connection pool and resolver for all 48 island SysBot endpoints."""

    def __init__(self):
        self._clients: Dict[str, SysBotClient] = {}
        self._lock = threading.Lock()

    def get_client(self, island_name: str, timeout: int = 5) -> SysBotClient:
        clean = re.sub(r"^sub\s+island\s+", "", (island_name or "").strip(), flags=re.IGNORECASE).strip().title()
        url = Config.get_island_sysbot_url(clean)
        cache_key = f"{clean.lower()}:{url}"
        with self._lock:
            if cache_key not in self._clients:
                self._clients[cache_key] = SysBotClient(
                    base_url=url,
                    api_key=getattr(Config, "SYSBOT_API_KEY", "") or "",
                    timeout=timeout,
                )
            return self._clients[cache_key]


_GLOBAL_CLIENT_POOL = SysBotClientPool()


def get_island_sysbot_client(island_name: str, timeout: int = 5) -> SysBotClient:
    """Obtain a pooled SysBotClient instance for the specified island."""
    return _GLOBAL_CLIENT_POOL.get_client(island_name, timeout=timeout)


# ============================================================================
# MULTI-ISLAND LOAD BALANCER & FAILOVER ROUTER
# ============================================================================

class SysBotLoadBalancer:
    """
    Intelligent Load Balancer for the 48-island SysBot fleet.
    Distributes drops, villager injections, and orders across healthy online islands.

    Supported Strategies:
      - 'least_loaded' (default): Picks the island with the lowest load score
        combining visitor saturation (55%), queue depth (35%), and latency (10%).
      - 'round_robin': Stateful round-robin cycle advancing across healthy islands.
      - 'least_queue': Picks the island with the shortest pending command queue.
      - 'lowest_latency': Selects the fastest responding SysBot instance.
      - 'random_weighted': Weighted selection inversely proportional to load score.
    """

    def __init__(self, pool: Optional[SysBotClientPool] = None):
        self._pool = pool or _GLOBAL_CLIENT_POOL
        self._round_robin_indices: Dict[str, int] = {}
        self._lock = threading.Lock()
        self._stats: Dict[str, int] = {
            "routes_least_loaded": 0,
            "routes_round_robin": 0,
            "routes_least_queue": 0,
            "routes_lowest_latency": 0,
            "failovers_executed": 0,
        }

    def select_island(
        self,
        group: str = "all",
        strategy: str = "least_loaded",
        candidates: Optional[List[str]] = None,
        max_visitors: int = 7,
    ) -> Optional[str]:
        """
        Select the best island name using the specified strategy.
        Returns None if no eligible island is online.
        """
        from utils.sysbot_telemetry import get_all_islands_health, get_healthy_islands

        strat = (strategy or "least_loaded").strip().lower()

        # Resolve candidate pool
        if candidates is not None and len(candidates) > 0:
            target_candidates = [str(c).strip() for c in candidates if str(c).strip()]
        else:
            target_candidates = get_healthy_islands(group=group, max_visitors=max_visitors)

        if not target_candidates:
            # Fallback: if all max_visitors exceeded, try any online island
            all_health = get_all_islands_health()
            all_online = [
                m["island"] for m in all_health.values()
                if m.get("is_online") and (candidates is None or m["island"] in candidates)
            ]
            if not all_online:
                return None
            target_candidates = all_online

        all_health = get_all_islands_health()
        valid_nodes: List[Dict[str, Any]] = []
        for name in target_candidates:
            metrics = all_health.get(name.lower())
            if metrics and metrics.get("is_online"):
                valid_nodes.append(metrics)
            elif not all_health:  # Telemetry cache might still be cold on startup
                valid_nodes.append({"island": name, "load_score": 0.5, "latency_ms": 50.0, "queue_count": 0})

        if not valid_nodes:
            return target_candidates[0] if target_candidates else None

        # Strategy 1: Round Robin
        if strat == "round_robin":
            with self._lock:
                idx = self._round_robin_indices.get(group.lower(), 0)
                selected = valid_nodes[idx % len(valid_nodes)]["island"]
                self._round_robin_indices[group.lower()] = (idx + 1) % len(valid_nodes)
                self._stats["routes_round_robin"] += 1
            return selected

        # Strategy 2: Least Queue
        if strat == "least_queue":
            sorted_nodes = sorted(valid_nodes, key=lambda n: (n.get("queue_count", 0), n.get("load_score", 1.0)))
            with self._lock:
                self._stats["routes_least_queue"] += 1
            return sorted_nodes[0]["island"]

        # Strategy 3: Lowest Latency
        if strat == "lowest_latency":
            sorted_nodes = sorted(valid_nodes, key=lambda n: n.get("latency_ms", 9999.0))
            with self._lock:
                self._stats["routes_lowest_latency"] += 1
            return sorted_nodes[0]["island"]

        # Strategy 4: Random Weighted
        if strat in ("random", "random_weighted"):
            import random
            weights = [max(0.01, 1.05 - n.get("load_score", 0.5)) for n in valid_nodes]
            chosen = random.choices(valid_nodes, weights=weights, k=1)[0]
            return chosen["island"]

        # Default Strategy: Least Loaded
        sorted_nodes = sorted(
            valid_nodes,
            key=lambda n: (
                n.get("load_score", 1.0),
                -n.get("health_score", 0.0),
                n.get("queue_count", 0),
            )
        )
        with self._lock:
            self._stats["routes_least_loaded"] += 1
        return sorted_nodes[0]["island"]

    def get_client(
        self,
        group: str = "all",
        strategy: str = "least_loaded",
        candidates: Optional[List[str]] = None,
        timeout: int = 5,
    ) -> Tuple[SysBotClient, str]:
        """
        Select an island and return its pooled SysBotClient instance and island name.
        """
        chosen_island = self.select_island(group=group, strategy=strategy, candidates=candidates)
        if not chosen_island:
            # Fallback to configured default island
            chosen_island = getattr(Config, "ORDER_BOT_ISLAND", "Sinta") or "Sinta"

        client = self._pool.get_client(chosen_island, timeout=timeout)
        return client, chosen_island

    def execute_with_failover(
        self,
        action_callable,
        group: str = "all",
        strategy: str = "least_loaded",
        candidates: Optional[List[str]] = None,
        max_attempts: int = 3,
        timeout: int = 5,
    ) -> Tuple[Dict[str, Any], str]:
        """
        Execute an operation against the cluster with automatic failover.
        If the primary candidate fails (HTTP 502/503/504, timeout, or offline),
        the load balancer immediately routes to the next healthiest candidate.

        action_callable signature: (client: SysBotClient, island_name: str) -> dict
        """
        from utils.sysbot_telemetry import get_healthy_islands

        pool_candidates = list(candidates) if candidates else get_healthy_islands(group=group)
        if not pool_candidates:
            fallback = getattr(Config, "ORDER_BOT_ISLAND", "Sinta") or "Sinta"
            pool_candidates = [fallback]

        attempted: set[str] = set()
        last_resp: Dict[str, Any] = {"success": False, "error": "No healthy islands available."}
        last_island = pool_candidates[0]

        attempts = min(max_attempts, len(pool_candidates))
        for _ in range(attempts):
            remaining = [c for c in pool_candidates if c.lower() not in attempted]
            if not remaining:
                break

            chosen = self.select_island(group=group, strategy=strategy, candidates=remaining)
            if not chosen:
                chosen = remaining[0]

            attempted.add(chosen.lower())
            last_island = chosen
            client = self._pool.get_client(chosen, timeout=timeout)

            try:
                resp = action_callable(client, chosen)
                if isinstance(resp, dict):
                    # Check if connection refused or offline
                    if not resp.get("success") and resp.get("error") and (
                        "unreachable" in str(resp.get("error")).lower()
                        or "offline" in str(resp.get("error")).lower()
                        or "timed out" in str(resp.get("error")).lower()
                    ):
                        with self._lock:
                            self._stats["failovers_executed"] += 1
                        logger.warning(
                            f"[SysBotLoadBalancer] Request failed on {chosen} ({resp.get('error')}); failing over..."
                        )
                        last_resp = resp
                        continue
                    return resp, chosen
                return {"success": True, "data": resp}, chosen
            except Exception as exc:
                with self._lock:
                    self._stats["failovers_executed"] += 1
                logger.warning(f"[SysBotLoadBalancer] Exception on {chosen}: {exc}; failing over...")
                last_resp = {"success": False, "error": str(exc)}

        return last_resp, last_island

    def submit_drop_balanced(
        self,
        items: Union[str, List[str]],
        group: str = "sub",
        username: str = "WebUser",
        candidates: Optional[List[str]] = None,
        strategy: str = "least_loaded",
    ) -> Dict[str, Any]:
        """
        Load-balanced drop execution with automatic failover across candidate islands.
        """
        def _do_drop(client: SysBotClient, isl: str) -> dict:
            return client.submit_drop_sync(items=items, username=username)

        resp, island = self.execute_with_failover(
            _do_drop, group=group, strategy=strategy, candidates=candidates
        )
        if isinstance(resp, dict):
            resp["dispatched_island"] = island
        return resp

    def inject_villagers_balanced(
        self,
        villagers: Union[str, Dict[str, Any], List[Union[str, Dict[str, Any]]]],
        group: str = "sub",
        plot: Optional[int] = None,
        candidates: Optional[List[str]] = None,
        strategy: str = "least_loaded",
    ) -> Dict[str, Any]:
        """
        Load-balanced villager injection with automatic failover.
        """
        def _do_inject(client: SysBotClient, isl: str) -> dict:
            return client.inject_villagers_sync(villagers=villagers, plot=plot)

        resp, island = self.execute_with_failover(
            _do_inject, group=group, strategy=strategy, candidates=candidates
        )
        if isinstance(resp, dict):
            resp["dispatched_island"] = island
        return resp

    def submit_order_balanced(
        self,
        username: str,
        order_text: str = "",
        items: Optional[List[str]] = None,
        villager: Optional[str] = None,
        user_id: Optional[str] = None,
        order_id: Optional[str] = None,
        group: str = "all",
        strategy: str = "least_loaded",
    ) -> Dict[str, Any]:
        """
        Load-balanced order placement with automatic failover.
        """
        def _do_order(client: SysBotClient, isl: str) -> dict:
            parsed_order, parsed_villager = parse_order_input(order_text)
            payload: Dict[str, Any] = {"username": username}
            if parsed_order:
                payload["order"] = parsed_order
            elif items:
                payload["items"] = items
            if villager or parsed_villager:
                payload["villager"] = villager or parsed_villager
            if user_id:
                payload["user_id"] = str(user_id)
            if order_id:
                payload["order_id"] = str(order_id)
            data, code = client._post("/api/order", payload)
            if not isinstance(data, dict):
                return {"success": False, "error": f"HTTP {code}"}
            return data

        resp, island = self.execute_with_failover(
            _do_order, group=group, strategy=strategy
        )
        if isinstance(resp, dict):
            resp["dispatched_island"] = island
        return resp

    def get_stats(self) -> Dict[str, Any]:
        """Return load balancer metrics and counters."""
        with self._lock:
            stats_copy = dict(self._stats)
        from utils.sysbot_telemetry import get_island_load_overview
        overview = get_island_load_overview()
        return {
            "load_balancer": stats_copy,
            "cluster": overview,
        }


_GLOBAL_LOAD_BALANCER = SysBotLoadBalancer()


def get_sysbot_load_balancer() -> SysBotLoadBalancer:
    """Return the global SysBotLoadBalancer instance."""
    return _GLOBAL_LOAD_BALANCER


def select_best_island(
    group: str = "all",
    strategy: str = "least_loaded",
    candidates: Optional[List[str]] = None,
) -> Optional[str]:
    """Select the best island from the cluster using real-time telemetry."""
    return _GLOBAL_LOAD_BALANCER.select_island(group=group, strategy=strategy, candidates=candidates)


def get_balanced_sysbot_client(
    group: str = "all",
    strategy: str = "least_loaded",
    candidates: Optional[List[str]] = None,
    timeout: int = 5,
) -> Tuple[SysBotClient, str]:
    """Get a pooled SysBotClient connected to the best available island in the fleet."""
    return _GLOBAL_LOAD_BALANCER.get_client(group=group, strategy=strategy, candidates=candidates, timeout=timeout)



