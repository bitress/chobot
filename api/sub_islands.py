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


# Sign-in is Discord only (api/flask_api.py). Email/password and YouTube, Twitch and
# Patreon logins were removed; old links can still be unlinked here.

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
# OAUTH STATE (signed state for the Discord login)
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
