"""Who may use the website Mod Console, and how much of it.

    Islander (ADMIN_ROLE_ID) / Baby Islander / Discord admins  -> "full"
    Senior Mod                                                 -> "limited": Flights, XLog, Bundles
    everyone else (Baby Mods included)                         -> None
"""

from __future__ import annotations

from typing import Any, Optional

from utils.config import Config

FULL = "full"
LIMITED = "limited"
# Console sections a limited user may open (the frontend hides the rest)
LIMITED_SECTIONS = ("flights", "xlog", "bundles")
# /dashboard/api endpoints a limited user may call (everything under /api/control-panel is
# Flights / XLog and stays open to them)
LIMITED_DASHBOARD_ENDPOINTS = frozenset({"dashboard.api_bundle_stats"})


def _role_ids(*values: Any) -> set[str]:
    return {str(v) for v in values if str(v or "").isdigit() and str(v) != "0"}


def dashboard_access(user: Optional[dict]) -> Optional[str]:
    """FULL, LIMITED or None for a website user payload (roles are Discord role ids)."""
    if not user:
        return None
    if user.get("is_admin"):
        return FULL
    roles = {str(r) for r in (user.get("roles") or [])}
    if roles & _role_ids(Config.ADMIN_ROLE_ID, getattr(Config, "BABY_ISLANDER_ROLE_ID", None)):
        return FULL
    if roles & _role_ids(Config.SENIOR_MOD_ROLE_ID):
        return LIMITED
    return None
