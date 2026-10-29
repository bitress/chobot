"""
Sub Islands & Multi-Provider OAuth Module
Handles:
- Sub Island Tier Access Matrix (ChoSoup, y ChoSoup, ChoTato Club, ChoColate, ChoFries, y ChoColate, y ChoTato)
- Island Telemetry, Plots, Drop Status, and Remote Dispatch
- Multi-Provider OAuth: Twitch, Patreon, YouTube
- Account Linking & Unlinking
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import random
import re
import secrets
import string
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from flask import Blueprint, jsonify, redirect, request, session, url_for

from utils.auth_tokens import get_auth_user, make_auth_token, update_auth_user
from utils.config import Config
from utils.database import connect_db
from utils.unified_auth import (
    provision_or_link_unified_account,
    unlink_provider_from_unified_account,
    get_unified_account_by_id,
    register_native_user,
    authenticate_native_user,
    set_native_password,
)

logger = logging.getLogger("SubIslands")

sub_islands_bp = Blueprint("sub_islands", __name__)

# ============================================================================
# SUB ISLAND ACCESS MATRIX
# y - youtube
# ChoTato Club - twitch
# ChoSoup, ChoColate, ChoFries - patreon
# ============================================================================

SUB_ISLAND_ACCESS_MATRIX: Dict[str, List[str]] = {
    "Hiraya": ["ChoSoup", "y ChoSoup", "ChoColate", "ChoFries", "y ChoTato", "Server Booster"],
    "Tagumpay": ["ChoSoup", "y ChoSoup", "ChoTato Club", "y ChoColate"],
    "Bahaghari": ["ChoSoup", "y ChoSoup", "ChoTato Club", "ChoColate", "ChoFries", "y ChoTato"],
    "Alapaap": ["ChoSoup", "y ChoSoup", "y ChoColate", "Server Booster"],
    "Bituin": ["ChoSoup", "y ChoSoup", "ChoTato Club", "ChoColate", "ChoFries"],
    "Lakan": ["ChoSoup", "y ChoSoup", "ChoTato Club", "y ChoColate"],
    "Galak": ["ChoSoup", "y ChoSoup", "ChoTato Club", "ChoColate", "ChoFries"],
    "Aruga": ["ChoSoup", "y ChoSoup", "y ChoColate"],
    "Likha": ["ChoSoup", "y ChoSoup", "ChoTato Club", "ChoColate", "ChoFries"],
    "Dalisay": ["ChoSoup", "y ChoSoup", "y ChoColate"],
    "Marahuyo": ["ChoSoup", "y ChoSoup", "ChoTato Club", "ChoColate", "ChoFries"],
    "Diwa": ["ChoSoup", "y ChoSoup", "ChoTato Club", "y ChoColate"],
    "Bonita": ["ChoSoup", "y ChoSoup", "ChoTato Club", "ChoColate", "ChoFries"],
    "Gabay": ["ChoSoup", "y ChoSoup", "y ChoColate"],
    "Dakila": ["ChoSoup", "y ChoSoup", "ChoTato Club", "ChoColate", "ChoFries", "y ChoColate"],
    "Kalangitan": ["ChoSoup", "y ChoSoup"],
    "Malaya": ["ChoSoup", "y ChoSoup"],
    "Pangarap": ["ChoSoup", "y ChoSoup"],
    "Giliw": ["ChoSoup", "y ChoSoup", "ChoTato Club", "ChoColate", "ChoFries", "y ChoColate"],
    "Adhika": ["ChoSoup", "y ChoSoup"],
}

_CANONICAL_SUB_ISLANDS = {k.lower(): k for k in SUB_ISLAND_ACCESS_MATRIX}


ROLE_ID_TO_TIER_NAME: Dict[str, str] = {
    str(getattr(Config, "ROLE_CHOSOUP", 1031974843656192080)): "ChoSoup",
    str(getattr(Config, "ROLE_Y_CHOSOUP", 1048323845771247681)): "y ChoSoup",
    str(getattr(Config, "ROLE_CHOTATO_CLUB", 898373041799696454)): "ChoTato Club",
    str(getattr(Config, "ROLE_Y_CHOTATO_CLUB", 1048323845771247679)): "y ChoTato",
    str(getattr(Config, "ROLE_CHOCOLATE", 1039516557405081601)): "ChoColate",
    str(getattr(Config, "ROLE_Y_CHOCOLATE", 1048323845771247680)): "y ChoColate",
    str(getattr(Config, "ROLE_CHOFRIES", 1031974954113179729)): "ChoFries",
    str(getattr(Config, "ROLE_SERVER_BOOSTER", 760256226448638006)): "Server Booster",
}
TIER_NAME_TO_ROLE_ID: Dict[str, str] = {v: k for k, v in ROLE_ID_TO_TIER_NAME.items()}


def _normalize_user_roles(roles: List[Any]) -> set[str]:
    """Convert a mixture of role IDs and tier names to canonical tier names & original IDs."""
    result = set()
    for r in roles:
        s = str(r).strip()
        result.add(s)
        if s in ROLE_ID_TO_TIER_NAME:
            result.add(ROLE_ID_TO_TIER_NAME[s])
        elif s in TIER_NAME_TO_ROLE_ID:
            result.add(TIER_NAME_TO_ROLE_ID[s])
    return result


def _clean_island_name(island_name: Optional[str]) -> str:
    """Normalize raw or formatted island name to standard canonical title."""
    if not island_name:
        return ""
    cleaned = re.sub(r"^sub\s+island\s+", "", island_name.strip(), flags=re.IGNORECASE).strip()
    return _CANONICAL_SUB_ISLANDS.get(cleaned.lower(), cleaned.title())


def get_user_sub_tier(user: Optional[Dict[str, Any]]) -> Optional[int]:
    """
    Resolve numeric subscription tier for a user payload:
    - Tier 3: ChoSoup / y ChoSoup, admin, mod
    - Tier 2: ChoColate / y ChoColate
    - Tier 1: ChoTato Club / y ChoTato / ChoFries / Server Booster
    """
    if not user:
        return None
    if user.get("is_admin") or user.get("is_mod"):
        return 3

    roles = _normalize_user_roles(user.get("roles", []))
    if "ChoSoup" in roles or "y ChoSoup" in roles:
        return 3
    if "ChoColate" in roles or "y ChoColate" in roles:
        return 2
    if "ChoTato Club" in roles or "y ChoTato" in roles or "ChoFries" in roles or "Server Booster" in roles:
        return 1
    return None


def _get_user_tiers(user: Optional[Dict[str, Any]]) -> List[str]:
    """Return matching tier names from the user's assigned roles."""
    if not user:
        return []
    roles = _normalize_user_roles(user.get("roles", []))
    valid_tiers = {
        "ChoSoup", "y ChoSoup",
        "ChoColate", "y ChoColate",
        "ChoTato Club", "y ChoTato", "ChoFries"
    }
    return [r for r in valid_tiers if r in roles]


def _user_has_island_access(user: Optional[Dict[str, Any]], island_name: str) -> bool:
    """Check whether a user has access to a specific sub island."""
    if not user:
        return False
    from utils.island_access import has_island_access
    return has_island_access(user.get("roles", []), [], False, island_name=island_name, user=user)


# ============================================================================
# IN-MEMORY SUB ISLAND TELEMETRY & PLOT STORE
# ============================================================================

_SUB_ISLAND_STATES: Dict[str, Dict[str, Any]] = {}


def _get_island_state(island_name: str) -> Dict[str, Any]:
    clean = _clean_island_name(island_name)
    if clean not in _SUB_ISLAND_STATES:
        _SUB_ISLAND_STATES[clean] = {
            "island": clean,
            "gate_status": "ONLINE",
            "dodo_code": "".join(random.choices("ABCDEFGHJKLMNPQRSTUVWXYZ23456789", k=5)),
            "visitors": 0,
            "is_busy": False,
            "queue_count": 0,
            "readiness": "ready",
            "plots": [
                {"plot": i, "villager": f"Resident {i}", "status": "occupied"}
                for i in range(1, 11)
            ],
        }
    return _SUB_ISLAND_STATES[clean]


def _get_auth_from_request() -> Optional[Dict[str, Any]]:
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[len("Bearer "):].strip()
        return get_auth_user(token)
    return None


# ============================================================================
# SUB ISLAND TELEMETRY & DISPATCH ROUTES
# ============================================================================

@sub_islands_bp.route("/api/islands/<name>", methods=["GET"])
def get_sub_island_telemetry(name: str):
    clean = _clean_island_name(name)
    state = _get_island_state(clean)
    allowed = SUB_ISLAND_ACCESS_MATRIX.get(clean, [])
    return jsonify({
        "island": clean,
        "gate_status": state["gate_status"],
        "dodo_code": None,  # Sub island Dodo codes are hidden
        "visitors": state["visitors"],
        "allowed_tiers": allowed,
    })


@sub_islands_bp.route("/api/islands/<name>/plots", methods=["GET"])
def get_sub_island_plots(name: str):
    clean = _clean_island_name(name)
    state = _get_island_state(clean)
    plots = list(state["plots"])

    # Query live SysBot instance if reachable
    try:
        from utils.sysbot_api import get_island_sysbot_client
        client = get_island_sysbot_client(clean, timeout=2)
        v_data = client.get_villagers_sync()
        if v_data.get("success") and "villagers" in v_data:
            live_villagers = v_data.get("villagers") or []
            live_plots = []
            for i in range(10):
                v_name = live_villagers[i] if i < len(live_villagers) else None
                live_plots.append({
                    "plot": i + 1,
                    "villager": v_name or f"Resident {i + 1}",
                    "status": "occupied" if v_name else "empty",
                    "adoptable": bool(v_name),
                })
            plots = live_plots
            state["plots"] = live_plots
    except Exception as exc:
        logger.debug(f"Failed to query live plots for {clean}: {exc}")

    return jsonify({
        "island": clean,
        "plots": plots,
    })


@sub_islands_bp.route("/api/islands/<name>/villagers/inject", methods=["POST"])
def inject_sub_island_villager(name: str):
    auth_user = _get_auth_from_request()
    if not auth_user:
        return jsonify({"success": False, "error": "Authentication required"}), 401

    clean = _clean_island_name(name)
    if not _user_has_island_access(auth_user, clean):
        return jsonify({"success": False, "error": f"You do not have access to {clean}."}), 403

    from api.flask_api import _user_has_sub_rules
    if not auth_user.get("is_admin") and not _user_has_sub_rules(auth_user):
        return jsonify({
            "success": False,
            "error": "You must review and accept the Subscriber Island Rules first (role 1077997850165772398 required) before injecting villagers.",
            "code": "missing_sub_rules_acceptance",
            "requires_sub_rules": True,
        }), 403

    data = request.get_json() or {}
    villager_name = (data.get("villager") or data.get("villager_name") or "").strip()
    plot_num = data.get("plot") or data.get("plot_number")
    if plot_num is not None:
        try:
            plot_num = int(plot_num)
        except (ValueError, TypeError):
            plot_num = None

    if not villager_name:
        return jsonify({"success": False, "error": "Villager name is required."}), 400

    from utils.sysbot_api import get_island_sysbot_client
    client = get_island_sysbot_client(clean, timeout=3)
    resp = client.inject_villagers_sync(villager_name, plot=plot_num)

    # Log injection audit event
    status_str = "queued" if resp.get("success") else "failed"
    try:
        from utils.sysbot_telemetry import ensure_telemetry_schema
        ensure_telemetry_schema()
        with connect_db() as conn:
            conn.execute("""
                INSERT INTO villager_injections_log
                (island_name, villager_name, plot_number, requested_by_user_id, status, response_message)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                clean,
                villager_name,
                plot_num or 0,
                str(auth_user.get("user_id") or ""),
                status_str,
                resp.get("message") or resp.get("error") or "",
            ))
    except Exception as exc:
        logger.debug(f"Audit log error: {exc}")

    # Optimistic in-memory update
    if plot_num and 1 <= plot_num <= 10:
        state = _get_island_state(clean)
        state["plots"][plot_num - 1] = {
            "plot": plot_num,
            "villager": villager_name,
            "status": "in_boxes",
            "adoptable": True,
        }

    return jsonify({
        "success": bool(resp.get("success")),
        "island": clean,
        "villager": villager_name,
        "plot": plot_num,
        "response": resp,
    })


@sub_islands_bp.route("/api/islands/<name>/drop-status", methods=["GET"])
def get_sub_island_drop_status(name: str):
    clean = _clean_island_name(name)
    state = _get_island_state(clean)
    return jsonify({
        "island": clean,
        "is_busy": state["is_busy"],
        "queue_count": state["queue_count"],
        "readiness": state["readiness"],
    })


@sub_islands_bp.route("/api/order/drop-sub", methods=["POST"])
def drop_sub_island():
    auth_user = _get_auth_from_request()
    if not auth_user:
        return jsonify({"success": False, "error": "Authentication required"}), 401

    data = request.get_json() or {}
    island_name = data.get("island_name") or data.get("island_id") or ""
    clean = _clean_island_name(island_name)

    if not clean or clean.lower() in ("auto", "any", "best"):
        from utils.sysbot_api import select_best_island
        allowed_islands = [
            isl for isl in (getattr(Config, "SUB_ISLANDS", []) or [])
            if _user_has_island_access(auth_user, isl)
        ]
        best_candidate = select_best_island(group="sub", candidates=allowed_islands)
        if best_candidate:
            clean = best_candidate
        elif allowed_islands:
            clean = allowed_islands[0]

    if not clean or not _user_has_island_access(auth_user, clean):
        return jsonify({"success": False, "error": f"You do not have access to {clean or 'the requested island'}."}), 403

    from api.flask_api import _user_has_sub_rules
    if not auth_user.get("is_admin") and not _user_has_sub_rules(auth_user):
        return jsonify({
            "success": False,
            "error": "You must review and accept the Subscriber Island Rules first (role 1077997850165772398 required) before dropping items.",
            "code": "missing_sub_rules_acceptance",
            "requires_sub_rules": True,
        }), 403

    command = (data.get("command") or "").strip()
    state = _get_island_state(clean)

    from utils.sysbot_api import get_island_sysbot_client
    client = get_island_sysbot_client(clean, timeout=3)

    if command.startswith("!drop"):
        # Presence check: verify user is physically on the island before dropping
        from utils.island_presence import verify_user_on_island
        is_present, matched_ign, current_visitors = verify_user_on_island(
            clean, user=auth_user, request_data=data, allow_admin_bypass=True
        )
        if not is_present:
            return jsonify({
                "success": False,
                "error": f"You must be on {clean} before dropping items. We checked via the SysBot API and your character was not detected on the island. Please fly to {clean} first and step outside the airport gates.",
                "code": "not_on_island",
                "island_name": clean,
                "visitors": current_visitors,
            }), 403

        items_part = command[5:].strip()
        from utils.data_manager import resolve_drop_items
        hex_str, hex_list = resolve_drop_items(items_part)
        item_count = len(hex_list) if hex_list else 1
        resp = client.submit_drop_sync(hex_str or items_part, username=auth_user.get("username", "WebUser"))
        is_success = bool(resp.get("success"))
        err_text = resp.get("error") or resp.get("message") or f"Drop command failed on {clean}."
        succ_text = resp.get("message") or f"Drop command dispatched silently to {clean}."
        return jsonify({
            "success": is_success,
            "island_name": clean,
            "item_count": item_count,
            "message": succ_text if is_success else err_text,
            "error": None if is_success else err_text,
            "sysbot_response": resp,
        }), (200 if is_success else 400)
    elif command.startswith("!inject"):
        parts = command[7:].strip().split()
        villager_name = parts[0] if parts else "Raymond"
        plot_num = data.get("plot_number")
        if plot_num is None and len(parts) > 1 and parts[1].isdigit():
            plot_num = int(parts[1])

        from utils.data_manager import resolve_villager_code
        villager_code = resolve_villager_code(villager_name)
        resp = client.inject_villagers_sync(villager_code, plot=plot_num)

        if plot_num is not None:
            try:
                p_idx = int(plot_num) - 1
                if 0 <= p_idx < 10:
                    state["plots"][p_idx]["villager"] = villager_name
                    state["plots"][p_idx]["status"] = "in_boxes"
            except (ValueError, TypeError):
                pass

        return jsonify({
            "success": True,
            "island_name": clean,
            "villager_name": villager_name,
            "plot_number": plot_num,
            "message": f"Villager {villager_name} injected into plot {plot_num} on {clean}.",
            "sysbot_response": resp,
        })
    elif command.startswith("!clean"):
        resp = client.submit_clean_sync()
        return jsonify({
            "success": True,
            "island_name": clean,
            "message": f"Clean command dispatched to {clean}.",
            "sysbot_response": resp,
        })

    return jsonify({
        "success": True,
        "island_name": clean,
        "message": f"Command dispatched to {clean}.",
    })


@sub_islands_bp.route("/api/islands/<name>/clean", methods=["POST"])
def clean_sub_island(name: str):
    auth_user = _get_auth_from_request()
    if not auth_user:
        return jsonify({"success": False, "error": "Authentication required"}), 401

    clean = _clean_island_name(name)
    if not _user_has_island_access(auth_user, clean):
        return jsonify({"success": False, "error": f"You do not have access to {clean}."}), 403

    from utils.sysbot_api import get_island_sysbot_client
    client = get_island_sysbot_client(clean, timeout=3)
    resp = client.submit_clean_sync()

    return jsonify({
        "success": True,
        "island": clean,
        "message": f"Clean command dispatched to {clean}.",
        "sysbot_response": resp,
    })


@sub_islands_bp.route("/api/islands/<name>/reset", methods=["POST"])
def reset_sub_island(name: str):
    auth_user = _get_auth_from_request()
    if not auth_user:
        return jsonify({"success": False, "error": "Authentication required"}), 401

    clean = _clean_island_name(name)
    is_mod_or_admin = bool(auth_user.get("is_mod") or auth_user.get("is_admin"))
    if not is_mod_or_admin and not _user_has_island_access(auth_user, clean):
        return jsonify({"success": False, "error": f"You do not have access to reset {clean}."}), 403

    from utils.sysbot_api import get_island_sysbot_client
    client = get_island_sysbot_client(clean, timeout=5)
    resp = client.refresh_dodo_sync()

    return jsonify({
        "success": True,
        "island": clean,
        "message": f"Reset / Dodo refresh dispatched to {clean}.",
        "sysbot_response": resp,
    })


# ============================================================================
# NATIVE ACCOUNT AUTHENTICATION (EMAIL & PASSWORD)
# ============================================================================

@sub_islands_bp.route("/api/auth/register", methods=["POST"])
def auth_register_native():
    data = request.get_json(silent=True) or {}
    email = data.get("email") or ""
    password = data.get("password") or ""
    username = data.get("username") or ""

    user, err = register_native_user(email=email, password=password, username=username)
    if err or not user:
        return jsonify({"success": False, "error": err or "Failed to register account."}), 400

    token = make_auth_token(user)
    return jsonify({
        "success": True,
        "token": token,
        "user": user,
    }), 201


@sub_islands_bp.route("/api/auth/login", methods=["POST"])
def auth_login_native():
    data = request.get_json(silent=True) or {}
    email = data.get("email") or ""
    password = data.get("password") or ""

    user, err = authenticate_native_user(email=email, password=password)
    if err or not user:
        return jsonify({"success": False, "error": err or "Invalid credentials."}), 401

    token = make_auth_token(user)
    return jsonify({
        "success": True,
        "token": token,
        "user": user,
    }), 200


@sub_islands_bp.route("/api/auth/password", methods=["POST"])
def auth_set_password():
    auth_user = _get_auth_from_request()
    if not auth_user:
        return jsonify({"success": False, "error": "Authentication required."}), 401

    data = request.get_json(silent=True) or {}
    new_password = data.get("new_password") or data.get("password") or ""
    old_password = data.get("old_password")

    ok, msg = set_native_password(
        user_id=str(auth_user.get("user_id") or ""),
        new_password=new_password,
        old_password=old_password,
    )
    if not ok:
        return jsonify({"success": False, "error": msg}), 400

    return jsonify({"success": True, "message": msg}), 200


# ============================================================================
# ACCOUNT LINKING & UNLINKING
# ============================================================================

@sub_islands_bp.route("/api/auth/link/<provider>", methods=["GET", "POST"])
def link_account(provider: str):
    auth_header = request.headers.get("Authorization", "")
    token = auth_header[len("Bearer "):].strip() if auth_header.startswith("Bearer ") else request.args.get("auth_token", "").strip()
    auth_user = get_auth_user(token) if token else _get_auth_from_request()
    if not auth_user:
        return jsonify({"success": False, "error": "Authentication required"}), 401

    provider = provider.lower()
    if provider == "discord":
        return jsonify({"success": False, "error": "Discord is your primary account and cannot be linked as a secondary platform."}), 400

    token_param = f"&auth_token={urllib.parse.quote(token)}" if token else ""
    return jsonify({
        "success": True,
        "provider": provider,
        "link_url": f"/api/auth/{provider}?link=true{token_param}",
    })


@sub_islands_bp.route("/api/auth/unlink/<provider>", methods=["DELETE", "POST"])
def unlink_account(provider: str):
    auth_header = request.headers.get("Authorization", "")
    token = auth_header[len("Bearer "):].strip() if auth_header.startswith("Bearer ") else request.args.get("auth_token", "").strip()
    if not token:
        return jsonify({"success": False, "error": "Authentication required"}), 401

    auth_user = get_auth_user(token)
    if not auth_user:
        return jsonify({"success": False, "error": "Authentication required"}), 401

    provider = provider.lower()
    if provider == "discord":
        return jsonify({"success": False, "error": "Discord is your primary account and cannot be unlinked."}), 400

    user_id = str(auth_user.get("user_id") or "")

    # Perform DB unlink on unified account
    updated = unlink_provider_from_unified_account(user_id, provider)
    if updated:
        for k in ("is_admin", "is_mod"):
            if auth_user.get(k):
                updated[k] = auth_user[k]
        update_auth_user(token, updated)
        return jsonify({
            "success": True,
            "unlinked": provider,
            "user": updated,
        })

    connected = dict(auth_user.get("connected_accounts") or {})
    connected.pop(provider, None)
    auth_user["connected_accounts"] = connected
    update_auth_user(token, auth_user)

    return jsonify({
        "success": True,
        "unlinked": provider,
    })


# ============================================================================
# MULTI-PROVIDER OAUTH: YOUTUBE, PATREON, TWITCH
# ============================================================================

def make_oauth_state(
    return_to: str,
    active_user_id: Optional[str] = None,
    link_token: Optional[str] = None,
) -> str:
    """Pack return_to, optional active_user_id, and optional link_token into a tamper-proof signed state token."""
    payload = {
        "nonce": secrets.token_hex(12),
        "ret": return_to,
        "uid": active_user_id or "",
        "link": link_token or "",
        "ts": int(time.time()),
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    secret = (getattr(Config, "FLASK_SECRET_KEY", None) or getattr(Config, "DASHBOARD_SECRET", None) or "cp_secret").encode("utf-8")
    sig = hmac.new(secret, raw, hashlib.sha256).hexdigest()[:16]
    b64 = base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")
    return f"{b64}.{sig}"


def verify_oauth_state(
    state_str: Optional[str],
    fallback_return: str = "https://www.chopaeng.com/auth/callback",
    saved_state: Optional[str] = None,
) -> Tuple[bool, str, Optional[str], Optional[str]]:
    """Verify OAuth state token statelessly via HMAC, with optional session fallback.

    Returns:
        (is_valid, return_to, active_user_id, link_token)
    """
    if not state_str:
        return False, fallback_return, None, None

    # 1. Stateless cryptographic HMAC verification
    if "." in state_str:
        try:
            b64, sig = state_str.split(".", 1)
            padding = "=" * ((4 - len(b64) % 4) % 4)
            raw = base64.urlsafe_b64decode(b64 + padding)
            secret = (getattr(Config, "FLASK_SECRET_KEY", None) or getattr(Config, "DASHBOARD_SECRET", None) or "cp_secret").encode("utf-8")
            expected_sig = hmac.new(secret, raw, hashlib.sha256).hexdigest()[:16]
            if hmac.compare_digest(sig, expected_sig):
                payload = json.loads(raw.decode("utf-8"))
                # Valid within 2 hours (7200s)
                if time.time() - payload.get("ts", 0) <= 7200:
                    ret = payload.get("ret") or fallback_return
                    uid = payload.get("uid") or None
                    link = payload.get("link") or None
                    return True, ret, uid, link
                else:
                    logger.warning("[OAuth State] Expired state token (ts=%s)", payload.get("ts"))
            else:
                logger.warning("[OAuth State] HMAC signature mismatch")
        except Exception as ex:
            logger.warning("[OAuth State] HMAC parse failed: %s", ex)

    # 2. Session-stored state fallback (backward compatibility with active sessions)
    if saved_state and hmac.compare_digest(state_str, saved_state):
        return True, fallback_return, None, None

    return False, fallback_return, None, None


def parse_oauth_state(
    state_str: Optional[str],
    fallback_return: str = "https://www.chopaeng.com/auth/callback",
) -> Tuple[str, Optional[str]]:
    """Verify and unpack signed state token. Returns (return_to, active_user_id)."""
    is_valid, ret, uid, _ = verify_oauth_state(state_str, fallback_return=fallback_return)
    if not is_valid:
        return fallback_return, None
    return ret, uid


# ── 1. YouTube OAuth (Google) ────────────────────────────────────────────────

@sub_islands_bp.route("/api/auth/youtube")
def auth_youtube():
    if not Config.YOUTUBE_CLIENT_ID:
        return jsonify({"error": "YouTube OAuth not configured"}), 503

    return_to = request.args.get("return_to", "https://www.chopaeng.com/auth/callback")
    auth_token = request.args.get("auth_token", "").strip()
    if not auth_token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            auth_token = auth_header[len("Bearer "):].strip()

    active_user_id = None
    if auth_token:
        curr_user = get_auth_user(auth_token)
        if curr_user:
            active_user_id = curr_user.get("user_id")

    state = make_oauth_state(return_to, active_user_id, link_token=auth_token)
    session["oauth_state_yt"] = state
    session["oauth_return_to_yt"] = return_to
    if active_user_id:
        session["oauth_active_uid_yt"] = active_user_id
    else:
        session.pop("oauth_active_uid_yt", None)

    redirect_uri = Config.YOUTUBE_REDIRECT_URI or url_for("sub_islands.auth_youtube_callback", _external=True)
    params = urllib.parse.urlencode({
        "client_id": Config.YOUTUBE_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile https://www.googleapis.com/auth/youtube.readonly",
        "state": state,
        "access_type": "offline",
        "prompt": "consent",
    })
    return redirect(f"https://accounts.google.com/o/oauth2/v2/auth?{params}")


@sub_islands_bp.route("/api/auth/youtube/callback")
def auth_youtube_callback():
    code = request.args.get("code")
    state = request.args.get("state")
    saved_state = session.pop("oauth_state_yt", None)
    sess_return = session.pop("oauth_return_to_yt", "https://www.chopaeng.com/auth/callback")
    sess_uid = session.pop("oauth_active_uid_yt", None)

    is_valid, return_to, state_uid, state_link_token = verify_oauth_state(
        state, fallback_return=sess_return, saved_state=saved_state
    )
    active_uid = state_uid or sess_uid
    if not active_uid and state_link_token:
        try:
            tok_payload = get_auth_user(state_link_token)
            if tok_payload:
                active_uid = tok_payload.get("user_id")
        except Exception:
            pass

    if not is_valid:
        return redirect(f"{return_to}?error=invalid_state")
    if not code:
        return redirect(f"{return_to}?error=missing_code")

    try:
        # Token Exchange
        token_req = urllib.request.Request(
            "https://oauth2.googleapis.com/token",
            data=urllib.parse.urlencode({
                "client_id": Config.YOUTUBE_CLIENT_ID,
                "client_secret": Config.YOUTUBE_CLIENT_SECRET,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": Config.YOUTUBE_REDIRECT_URI or url_for("sub_islands.auth_youtube_callback", _external=True),
            }).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(token_req, timeout=10) as resp:
            token_data = json.loads(resp.read().decode("utf-8"))

        access_token = token_data.get("access_token")
        if not access_token:
            return redirect(f"{return_to}?error=token_exchange_failed")

        # User Info
        user_req = urllib.request.Request(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        with urllib.request.urlopen(user_req, timeout=10) as resp:
            user_data = json.loads(resp.read().decode("utf-8"))

        yt_id = str(user_data.get("id") or "")
        yt_name = user_data.get("name") or user_data.get("email") or "YouTube User"
        yt_pfp = user_data.get("picture") or ""
        yt_email = user_data.get("email") or ""

        # Membership Check
        roles: List[str] = []
        try:
            mem_url = "https://www.googleapis.com/youtube/v3/members?part=snippet"
            if Config.YOUTUBE_CHANNEL_ID:
                mem_url += f"&filterByMemberChannelId={Config.YOUTUBE_CHANNEL_ID}"
            mem_req = urllib.request.Request(
                mem_url,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            with urllib.request.urlopen(mem_req, timeout=10) as resp:
                mem_data = json.loads(resp.read().decode("utf-8"))

            for item in mem_data.get("items", []):
                snippet = item.get("snippet", {})
                level_title = snippet.get("membershipsDetails", {}).get("highestAccessibleLevelDisplayName", "")
                if "ChoSoup" in level_title:
                    roles.append("y ChoSoup")
                elif "ChoColate" in level_title:
                    roles.append("y ChoColate")
                elif "ChoTato" in level_title:
                    roles.append("y ChoTato")
        except Exception as ex:
            logger.warning("[YouTube] Membership fetch note: %s", ex)

        unified_user = provision_or_link_unified_account(
            provider="youtube",
            provider_user_id=yt_id,
            username=yt_name,
            avatar=yt_pfp,
            email=yt_email,
            roles=roles,
            sub_tier=roles[0] if roles else None,
            raw_profile=user_data,
            active_user_id=active_uid,
        )
        token = make_auth_token(unified_user)
        if state_link_token:
            try:
                update_auth_user(state_link_token, unified_user)
            except Exception:
                pass
        return redirect(f"{return_to}?token={urllib.parse.quote(token)}")
    except Exception as exc:
        logger.error("[YouTube OAuth] Failed: %s", exc)
        return redirect(f"{return_to}?error=youtube_auth_failed")


# ── 2. Patreon OAuth ─────────────────────────────────────────────────────────

@sub_islands_bp.route("/api/auth/patreon")
def auth_patreon():
    if not Config.PATREON_CLIENT_ID:
        return jsonify({"error": "Patreon OAuth not configured"}), 503

    return_to = request.args.get("return_to", "https://www.chopaeng.com/auth/callback")
    auth_token = request.args.get("auth_token", "").strip()
    if not auth_token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            auth_token = auth_header[len("Bearer "):].strip()

    active_user_id = None
    if auth_token:
        curr_user = get_auth_user(auth_token)
        if curr_user:
            active_user_id = curr_user.get("user_id")

    state = make_oauth_state(return_to, active_user_id, link_token=auth_token)
    session["oauth_state_patreon"] = state
    session["oauth_return_to_patreon"] = return_to
    if active_user_id:
        session["oauth_active_uid_patreon"] = active_user_id
    else:
        session.pop("oauth_active_uid_patreon", None)

    redirect_uri = Config.PATREON_REDIRECT_URI or url_for("sub_islands.auth_patreon_callback", _external=True)
    params = urllib.parse.urlencode({
        "client_id": Config.PATREON_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "identity identity[email] identity.memberships",
        "state": state,
    })
    return redirect(f"https://www.patreon.com/oauth2/authorize?{params}")


@sub_islands_bp.route("/api/auth/patreon/callback")
def auth_patreon_callback():
    code = request.args.get("code")
    state = request.args.get("state")
    saved_state = session.pop("oauth_state_patreon", None)
    sess_return = session.pop("oauth_return_to_patreon", "https://www.chopaeng.com/auth/callback")
    sess_uid = session.pop("oauth_active_uid_patreon", None)

    is_valid, return_to, state_uid, state_link_token = verify_oauth_state(
        state, fallback_return=sess_return, saved_state=saved_state
    )
    active_uid = state_uid or sess_uid
    if not active_uid and state_link_token:
        try:
            tok_payload = get_auth_user(state_link_token)
            if tok_payload:
                active_uid = tok_payload.get("user_id")
        except Exception:
            pass

    if not is_valid:
        return redirect(f"{return_to}?error=invalid_state")
    if not code:
        return redirect(f"{return_to}?error=missing_code")

    try:
        # Token Exchange
        token_req = urllib.request.Request(
            "https://www.patreon.com/api/oauth2/token",
            data=urllib.parse.urlencode({
                "code": code,
                "grant_type": "authorization_code",
                "client_id": Config.PATREON_CLIENT_ID,
                "client_secret": Config.PATREON_CLIENT_SECRET,
                "redirect_uri": Config.PATREON_REDIRECT_URI or url_for("sub_islands.auth_patreon_callback", _external=True),
            }).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(token_req, timeout=10) as resp:
            token_data = json.loads(resp.read().decode("utf-8"))

        access_token = token_data.get("access_token")
        if not access_token:
            return redirect(f"{return_to}?error=token_exchange_failed")

        # Identity & Tiers
        ident_url = (
            "https://www.patreon.com/api/oauth2/v2/identity"
            "?include=memberships.currently_entitled_tiers"
            "&fields[user]=full_name,image_url,email"
            "&fields[tier]=title"
        )
        user_req = urllib.request.Request(
            ident_url,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        with urllib.request.urlopen(user_req, timeout=10) as resp:
            user_data = json.loads(resp.read().decode("utf-8"))

        pat_id = str(user_data.get("data", {}).get("id") or "")
        pat_name = user_data.get("data", {}).get("attributes", {}).get("full_name") or "Patreon Supporter"
        pat_avatar = user_data.get("data", {}).get("attributes", {}).get("image_url") or ""
        pat_email = user_data.get("data", {}).get("attributes", {}).get("email") or ""

        from utils.tier_sync import patreon_tier_roles
        roles: List[str] = patreon_tier_roles(
            (inc.get("attributes") or {}).get("title") or ""
            for inc in user_data.get("included", [])
            if inc.get("type") == "tier"
        )

        unified_user = provision_or_link_unified_account(
            provider="patreon",
            provider_user_id=pat_id,
            username=pat_name,
            avatar=pat_avatar,
            email=pat_email,
            roles=roles,
            sub_tier=roles[0] if roles else None,
            raw_profile=user_data,
            active_user_id=active_uid,
        )
        token = make_auth_token(unified_user)
        if state_link_token:
            try:
                update_auth_user(state_link_token, unified_user)
            except Exception:
                pass
        return redirect(f"{return_to}?token={urllib.parse.quote(token)}")
    except Exception as exc:
        logger.error("[Patreon OAuth] Failed: %s", exc)
        return redirect(f"{return_to}?error=patreon_auth_failed")


@sub_islands_bp.route("/api/webhooks/patreon", methods=["POST"])
def patreon_members_webhook():
    """Patreon members:* webhook: a new, changed or cancelled pledge updates the linked
    account's tiers right away (the scheduled sync in utils/tier_sync.py catches the rest)."""
    from utils.tier_sync import apply_patreon_webhook, verify_patreon_webhook

    body = request.get_data()
    if not verify_patreon_webhook(body, request.headers.get("X-Patreon-Signature")):
        return jsonify({"error": "invalid signature"}), 401
    try:
        user_id = apply_patreon_webhook(json.loads(body or b"{}"))
    except Exception as exc:
        logger.warning("[Patreon webhook] %s failed: %s", request.headers.get("X-Patreon-Event"), exc)
        return jsonify({"error": "could not apply"}), 500
    logger.info("[Patreon webhook] %s for Patreon user %s", request.headers.get("X-Patreon-Event"), user_id)
    return jsonify({"ok": True})


# ── 3. Twitch OAuth ──────────────────────────────────────────────────────────

@sub_islands_bp.route("/api/auth/twitch")
def auth_twitch():
    if not Config.TWITCH_CLIENT_ID:
        return jsonify({"error": "Twitch OAuth not configured"}), 503

    return_to = request.args.get("return_to", "https://www.chopaeng.com/auth/callback")
    auth_token = request.args.get("auth_token", "").strip()
    if not auth_token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            auth_token = auth_header[len("Bearer "):].strip()

    active_user_id = None
    if auth_token:
        curr_user = get_auth_user(auth_token)
        if curr_user:
            active_user_id = curr_user.get("user_id")

    state = make_oauth_state(return_to, active_user_id, link_token=auth_token)
    session["oauth_state_twitch"] = state
    session["oauth_return_to_twitch"] = return_to
    if active_user_id:
        session["oauth_active_uid_twitch"] = active_user_id
    else:
        session.pop("oauth_active_uid_twitch", None)

    redirect_uri = Config.TWITCH_REDIRECT_URI or url_for("sub_islands.auth_twitch_callback", _external=True)
    params = urllib.parse.urlencode({
        "client_id": Config.TWITCH_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "user:read:subscriptions user:read:email",
        "state": state,
    })
    return redirect(f"https://id.twitch.tv/oauth2/authorize?{params}")


@sub_islands_bp.route("/api/auth/twitch/callback")
def auth_twitch_callback():
    code = request.args.get("code")
    state = request.args.get("state")
    saved_state = session.pop("oauth_state_twitch", None)
    sess_return = session.pop("oauth_return_to_twitch", "https://www.chopaeng.com/auth/callback")
    sess_uid = session.pop("oauth_active_uid_twitch", None)

    is_valid, return_to, state_uid, state_link_token = verify_oauth_state(
        state, fallback_return=sess_return, saved_state=saved_state
    )
    active_uid = state_uid or sess_uid
    if not active_uid and state_link_token:
        try:
            tok_payload = get_auth_user(state_link_token)
            if tok_payload:
                active_uid = tok_payload.get("user_id")
        except Exception:
            pass

    if not is_valid:
        return redirect(f"{return_to}?error=invalid_state")
    if not code:
        return redirect(f"{return_to}?error=missing_code")

    try:
        # Token Exchange
        token_req = urllib.request.Request(
            "https://id.twitch.tv/oauth2/token",
            data=urllib.parse.urlencode({
                "client_id": Config.TWITCH_CLIENT_ID,
                "client_secret": Config.TWITCH_CLIENT_SECRET,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": Config.TWITCH_REDIRECT_URI or url_for("sub_islands.auth_twitch_callback", _external=True),
            }).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(token_req, timeout=10) as resp:
            token_data = json.loads(resp.read().decode("utf-8"))

        access_token = token_data.get("access_token")
        if not access_token:
            return redirect(f"{return_to}?error=token_exchange_failed")

        # Twitch User Profile
        user_req = urllib.request.Request(
            "https://api.twitch.tv/helix/users",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Client-Id": Config.TWITCH_CLIENT_ID,
            },
        )
        with urllib.request.urlopen(user_req, timeout=10) as resp:
            user_data = json.loads(resp.read().decode("utf-8"))

        tw_item = (user_data.get("data") or [{}])[0]
        tw_id = str(tw_item.get("id") or "")
        tw_name = tw_item.get("display_name") or tw_item.get("login") or "Twitch User"
        tw_pfp = tw_item.get("profile_image_url") or ""
        tw_email = tw_item.get("email") or ""

        # Check Subscription to Broadcaster Channel
        roles: List[str] = []
        try:
            broadcaster_id = Config.TWITCH_BROADCASTER_ID
            sub_url = f"https://api.twitch.tv/helix/subscriptions/user?broadcaster_id={broadcaster_id}&user_id={tw_id}"
            sub_req = urllib.request.Request(
                sub_url,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Client-Id": Config.TWITCH_CLIENT_ID,
                },
            )
            with urllib.request.urlopen(sub_req, timeout=10) as resp:
                sub_data = json.loads(resp.read().decode("utf-8"))

            if sub_data.get("data"):
                roles.append("ChoTato Club")
        except Exception as ex:
            logger.warning("[Twitch] Subscription fetch note: %s", ex)

        unified_user = provision_or_link_unified_account(
            provider="twitch",
            provider_user_id=tw_id,
            username=tw_name,
            avatar=tw_pfp,
            email=tw_email,
            roles=roles,
            sub_tier=roles[0] if roles else None,
            raw_profile=tw_item,
            active_user_id=active_uid,
        )
        token = make_auth_token(unified_user)
        if state_link_token:
            try:
                update_auth_user(state_link_token, unified_user)
            except Exception:
                pass
        return redirect(f"{return_to}?token={urllib.parse.quote(token)}")
    except Exception as exc:
        logger.error("[Twitch OAuth] Failed: %s", exc)
        return redirect(f"{return_to}?error=twitch_auth_failed")

