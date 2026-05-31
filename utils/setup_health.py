"""Self-hosted setup health checks for streamer-owned ChoBot installs."""

from __future__ import annotations

import os
from typing import Any

from utils.config import Config
from utils.database import connect_db
from utils.dodo_store import recent_dodo_captures
from utils.embed_templates import load_free_dodo_embed_template


def get_self_hosted_setup_status() -> dict[str, Any]:
    checks = [
        _check("Dashboard secret", bool(Config.DASHBOARD_SECRET), "Set DASHBOARD_SECRET so only you can open the dashboard."),
        _check("Discord token", bool(Config.DISCORD_TOKEN), "Set DISCORD_TOKEN from your Discord developer application."),
        _check("Discord server", bool(Config.GUILD_ID), "Set GUILD_ID for your Discord server."),
        _check("Member island category", bool(Config.CATEGORY_ID), "Set SUB_CATEGORY_ID for member/VIP island channels."),
        _check("Free island category", bool(Config.FREE_CATEGORY_ID), "Set FREE_CATEGORY_ID if you run free islands."),
        _check("OrderBot watch channels", bool(Config.ORDERBOT_CHANNEL_IDS), "Set ORDERBOT_CHANNEL_IDS to channels where Berichan/OrderBot posts Dodo codes."),
        _check("Flight listen channel", bool(Config.FLIGHT_LISTEN_CHANNEL_ID), "Set FLIGHT_LISTEN_CHANNEL_ID for arrival logs."),
        _check("Flight log channel", bool(Config.FLIGHT_LOG_CHANNEL_ID), "Set FLIGHT_LOG_CHANNEL_ID for moderation logs."),
        _check("Twitch channel", bool(Config.TWITCH_CHANNEL), "Set TWITCH_CHANNEL only if you want Twitch chat commands.", optional=True),
        _check("Twitch token", bool(Config.TWITCH_TOKEN), "Set TWITCH_TOKEN only if Twitch chat commands are enabled.", optional=True),
        _check("Item workbook", bool(Config.WORKBOOK_NAME), "Set WORKBOOK_NAME if you use Google Sheets item search."),
        _check("Dodo stale timer", bool(Config.DODO_STALE_MINUTES), f"Current stale timer: {Config.DODO_STALE_MINUTES} minutes.", optional=True),
        _database_check(),
        _path_check("Member island data folder", Config.VILLAGERS_DIR, "Set VILLAGERS_DIR if local island files are used."),
        _path_check("Free island data folder", Config.TWITCH_VILLAGERS_DIR, "Set TWITCH_VILLAGERS_DIR if local free-island files are used."),
    ]
    required = [check for check in checks if not check.get("optional")]
    ready = all(check["ok"] for check in required)
    try:
        captures = recent_dodo_captures(limit=8)
    except Exception:
        captures = []
    try:
        template = load_free_dodo_embed_template()
    except Exception:
        template = {}

    return {
        "ready": ready,
        "mode": "self_hosted",
        "summary": f"{sum(1 for check in checks if check['ok'])}/{len(checks)} checks passing",
        "checks": checks,
        "orderbot_channel_ids": Config.ORDERBOT_CHANNEL_IDS,
        "orderbot_author_ids": Config.ORDERBOT_AUTHOR_IDS,
        "recent_dodo_captures": captures,
        "free_dodo_embed_template": template,
    }


def _check(name: str, ok: bool, detail: str, optional: bool = False) -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": detail, "optional": optional}


def _database_check() -> dict[str, Any]:
    try:
        with connect_db() as db:
            db.execute("SELECT 1").fetchone()
        return _check("Database", True, f"Connected using DB_BACKEND={Config.DB_BACKEND}.")
    except Exception as exc:
        return _check("Database", False, f"Database connection failed: {exc}")


def _path_check(name: str, path: str | None, detail: str) -> dict[str, Any]:
    if not path:
        return _check(name, True, detail, optional=True)
    return _check(name, os.path.exists(path), f"{path}", optional=True)
