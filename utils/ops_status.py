"""Operational health, maintenance-mode, and backup helpers."""

from __future__ import annotations

import os
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from utils.config import Config
from utils.database import DEFAULT_SQLITE_PATH, connect_db, get_backend
from utils.db_migration import backup_sqlite_database

APP_STARTED_AT = time.time()
MAINTENANCE_KEYS = {
    "maintenance_mode": "false",
    "maintenance_disable_dodo_reveals": "false",
    "maintenance_disable_refresh": "false",
    "maintenance_disable_commands": "false",
    "maintenance_disable_ads": "false",
    "maintenance_islands": "{}",
    "maintenance_message": "",
}
_runtime_lock = threading.Lock()
_runtime_services: dict[str, dict[str, Any]] = {}
_data_manager = None
_backup_scheduler_started = False


def set_active_data_manager(data_manager) -> None:
    global _data_manager
    _data_manager = data_manager


def get_active_data_manager():
    return _data_manager


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def uptime_seconds() -> int:
    return max(int(time.time() - APP_STARTED_AT), 0)


def configured_services_payload() -> dict[str, bool]:
    return {
        "discord_token": bool(Config.DISCORD_TOKEN),
        "twitch_token": bool(Config.TWITCH_TOKEN),
        "patreon": bool(Config.PATREON_TOKEN and Config.PATREON_CAMPAIGN_ID),
        "google_sheets": bool(Config.WORKBOOK_NAME and os.path.exists(Config.JSON_KEYFILE)),
        "discord_oauth": bool(Config.DISCORD_CLIENT_ID and Config.DISCORD_CLIENT_SECRET),
        "r2": bool(Config.R2_ACCOUNT_ID and Config.R2_ACCESS_KEY_ID and Config.R2_SECRET_ACCESS_KEY),
        "openai": bool(Config.OPENAI_API_KEY),
        "gemini": bool(Config.GEMINI_API_KEY),
    }


def record_service_status(
    service: str,
    *,
    mode: str | None = None,
    status: str = "running",
    error: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Record an in-process service heartbeat/status snapshot."""
    now = utc_now_iso()
    with _runtime_lock:
        current = dict(_runtime_services.get(service, {}))
        current.update(
            {
                "service": service,
                "status": status,
                "last_heartbeat": now,
                "updated_at": now,
            }
        )
        if mode is not None:
            current["mode"] = mode
        if error:
            current["last_error"] = _safe_error(error)
            current["last_error_at"] = now
        if extra:
            current.update(extra)
        _runtime_services[service] = current


def service_statuses() -> dict[str, dict[str, Any]]:
    with _runtime_lock:
        return {key: dict(value) for key, value in _runtime_services.items()}


def _safe_error(error: str, limit: int = 300) -> str:
    cleaned = " ".join(str(error).split())
    return cleaned[:limit]


def _bool_from_setting(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _ensure_settings_table(db) -> None:
    db.execute(
        """CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )"""
    )


def get_maintenance_settings() -> dict[str, Any]:
    values = dict(MAINTENANCE_KEYS)
    try:
        db = connect_db()
        try:
            _ensure_settings_table(db)
            rows = db.execute(
                "SELECT key, value FROM settings WHERE key IN (?, ?, ?, ?, ?, ?, ?)",
                tuple(MAINTENANCE_KEYS),
            ).fetchall()
            for row in rows:
                values[row["key"]] = row["value"]
        finally:
            db.close()
    except Exception:
        pass

    try:
        islands = json.loads(values["maintenance_islands"] or "{}")
    except Exception:
        islands = {}
    return {
        "maintenance_mode": _bool_from_setting(values["maintenance_mode"]),
        "disable_dodo_reveals": _bool_from_setting(values["maintenance_disable_dodo_reveals"]),
        "disable_refresh": _bool_from_setting(values["maintenance_disable_refresh"]),
        "disable_commands": _bool_from_setting(values["maintenance_disable_commands"]),
        "disable_ads": _bool_from_setting(values["maintenance_disable_ads"]),
        "islands": islands if isinstance(islands, dict) else {},
        "message": str(values["maintenance_message"] or ""),
    }


AD_PROVIDERS = ("adsense", "nitropay", "none")


def get_ad_config() -> dict[str, str]:
    """Which ad network the website should load, from the `ad_provider` setting (edited by hand in the DB).

    `maintenance_disable_ads=true` wins over it, and anything unrecognised falls back to AdSense.
    """
    provider = "adsense"
    try:
        db = connect_db()
        try:
            _ensure_settings_table(db)
            row = db.execute("SELECT value FROM settings WHERE key = ?", ("ad_provider",)).fetchone()
            if row:
                value = str(row["value"] or "").strip().lower()
                if value in AD_PROVIDERS:
                    provider = value
        finally:
            db.close()
    except Exception:
        pass
    if get_maintenance_settings()["disable_ads"]:
        provider = "none"
    return {"provider": provider}


def update_maintenance_settings(payload: dict[str, Any]) -> dict[str, Any]:
    updates = {
        "maintenance_mode": "true" if _bool_from_setting(payload.get("maintenance_mode")) else "false",
        "maintenance_disable_dodo_reveals": "true"
        if _bool_from_setting(payload.get("disable_dodo_reveals"))
        else "false",
        "maintenance_disable_refresh": "true" if _bool_from_setting(payload.get("disable_refresh")) else "false",
        "maintenance_disable_commands": "true" if _bool_from_setting(payload.get("disable_commands")) else "false",
        "maintenance_disable_ads": "true" if _bool_from_setting(payload.get("disable_ads")) else "false",
        "maintenance_islands": json.dumps(payload.get("islands") if isinstance(payload.get("islands"), dict) else {}),
        "maintenance_message": str(payload.get("message") or "").strip()[:500],
    }
    db = connect_db()
    try:
        _ensure_settings_table(db)
        for key, value in updates.items():
            db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return get_maintenance_settings()


ISLAND_NOTICE_STATUSES = {
    "updating": "Offline for updates",
    "offline": "Temporarily offline",
}
_NOTICE_FIELDS = ("status", "message", "ends_at", "set_by", "set_at")
# Switches a notice turns on. Their earlier values are kept in "_before_notice" and restored on clear,
# so bringing an island online doesn't undo a reveal pause someone set separately.
_NOTICE_SWITCHES = ("disable_dodo_reveals", "queue_paused")


def island_notice(entry: Any) -> dict[str, Any] | None:
    """The member-facing part of a maintenance_islands entry, or None if no notice is set."""
    if not isinstance(entry, dict) or entry.get("status") not in ISLAND_NOTICE_STATUSES:
        return None
    return {
        "status": entry["status"],
        "label": ISLAND_NOTICE_STATUSES[entry["status"]],
        "message": str(entry.get("message") or ""),
        "ends_at": entry.get("ends_at"),
    }


def set_island_notices(keys: list[str], notice: dict[str, Any] | None) -> dict[str, Any]:
    """Set (or clear, with notice=None) the notice on each island key in maintenance_islands.

    Keys are clean_text() island names, the same keys the Dodo reveal and queue checks read.
    A notice also pauses Dodo reveals and the queue for that island; clearing removes only the
    notice fields, so other per-island switches stay as they were.
    """
    db = connect_db()
    try:
        _ensure_settings_table(db)
        row = db.execute("SELECT value FROM settings WHERE key = ?", ("maintenance_islands",)).fetchone()
        try:
            islands = json.loads((row["value"] if row else None) or "{}")
        except Exception:
            islands = {}
        if not isinstance(islands, dict):
            islands = {}
        for key in keys:
            if not key:
                continue
            entry = dict(islands.get(key)) if isinstance(islands.get(key), dict) else {}
            if notice is None:
                if "status" not in entry:
                    continue   # no notice here: leave other switches alone
                before = entry.pop("_before_notice", {}) if isinstance(entry.get("_before_notice"), dict) else {}
                entry = {k: v for k, v in entry.items() if k not in _NOTICE_FIELDS and k not in _NOTICE_SWITCHES}
                entry.update({k: v for k, v in before.items() if k in _NOTICE_SWITCHES})
            else:
                if "status" not in entry:
                    entry["_before_notice"] = {k: entry[k] for k in _NOTICE_SWITCHES if k in entry}
                entry = {**entry, **notice, **{k: True for k in _NOTICE_SWITCHES}}
            if entry:
                islands[key] = entry
            else:
                islands.pop(key, None)
        db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("maintenance_islands", json.dumps(islands)),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return islands


def database_health() -> dict[str, Any]:
    started = time.time()
    try:
        db = connect_db()
        try:
            db.execute("SELECT 1").fetchone()
        finally:
            db.close()
        return {
            "status": "ok",
            "backend": get_backend(),
            "latency_ms": round((time.time() - started) * 1000, 1),
        }
    except Exception as exc:
        return {
            "status": "error",
            "backend": get_backend(),
            "error": _safe_error(str(exc)),
        }


def cache_health(data_manager=None, fallback_loader=None) -> dict[str, Any]:
    cache = {}
    last_update = None
    refresh_interval_seconds = None
    source = "unavailable"
    manager_initialised = data_manager is not None

    refresh_attempt = None
    refresh_status = "unavailable"
    refresh_error = None

    if data_manager is not None:
        with data_manager.lock:
            cache = dict(data_manager.cache)
            last_update = data_manager.last_update
            refresh_interval_seconds = float(data_manager.cache_refresh_hours or 0) * 3600
            refresh_attempt = getattr(data_manager, "last_refresh_attempt", None)
            refresh_status = getattr(data_manager, "last_refresh_status", "unknown")
            refresh_error = getattr(data_manager, "last_refresh_error", None)
        source = "data_manager"
    elif fallback_loader:
        cache, last_update, refresh_interval_seconds, source = fallback_loader()

    item_count = len([key for key in cache if key != "_display"])
    age_seconds = None
    if last_update is not None:
        age_seconds = max(int(time.time() - last_update.timestamp()), 0)

    max_age = int(getattr(Config, "HEALTH_CACHE_MAX_AGE_SECONDS", 7200) or 7200)
    status = "ok"
    reasons = []
    if item_count <= 0:
        status = "error"
        reasons.append("item cache is empty")
    elif age_seconds is not None and age_seconds > max_age:
        status = "degraded"
        reasons.append("item cache is stale")
    elif last_update is None:
        status = "degraded"
        reasons.append("cache has no refresh timestamp")

    return {
        "status": status,
        "items": item_count,
        "last_update": last_update.isoformat() if last_update else None,
        "age_seconds": age_seconds,
        "max_age_seconds": max_age,
        "refresh_interval_seconds": refresh_interval_seconds,
        "source": source,
        "data_manager_initialised": manager_initialised,
        "last_refresh_attempt": refresh_attempt.isoformat() if refresh_attempt else None,
        "last_refresh_status": refresh_status,
        "last_refresh_error": _safe_error(refresh_error) if refresh_error else None,
        "reasons": reasons,
    }


def build_health_payload(
    *,
    data_manager=None,
    fallback_loader=None,
    include_private: bool = False,
) -> dict[str, Any]:
    db = database_health()
    cache = cache_health(data_manager, fallback_loader)
    maintenance = get_maintenance_settings()

    reasons = []
    if db["status"] != "ok":
        reasons.append("database unavailable")
    reasons.extend(cache.get("reasons", []))
    if maintenance["maintenance_mode"]:
        reasons.append("maintenance mode enabled")

    status = "ok"
    if db["status"] == "error" or cache["status"] == "error":
        status = "error"
    elif reasons or cache["status"] == "degraded":
        status = "degraded"

    payload = {
        "status": status,
        "timestamp": utc_now_iso(),
        "uptime_seconds": uptime_seconds(),
        "reasons": reasons,
        "database": db,
        "cache": cache,
        "maintenance": maintenance,
    }
    if include_private:
        payload["services"] = service_statuses()
        payload["integrations"] = configured_services_payload()
        payload["backup"] = {
            "backend": get_backend(),
            "backup_dir": safe_backup_dir_label(),
        }
    return payload


def backup_dir_path() -> str:
    configured = str(getattr(Config, "BACKUP_DIR", "backups") or "backups").strip()
    path = Path(configured)
    if not path.is_absolute():
        path = Path(os.path.dirname(DEFAULT_SQLITE_PATH)) / path
    return str(path)


def sqlite_database_path() -> str:
    return Config.SQLITE_DB_PATH or DEFAULT_SQLITE_PATH


def safe_backup_dir_label() -> str:
    path = Path(backup_dir_path())
    return path.name or "backups"


def create_sqlite_backup(reason: str = "manual") -> dict[str, Any]:
    if get_backend() != "sqlite":
        return {"ok": False, "backend": get_backend(), "skipped": True, "reason": "sqlite_only"}
    backup_path = backup_sqlite_database(sqlite_database_path(), backup_dir_path())
    return {
        "ok": True,
        "backend": "sqlite",
        "reason": reason,
        "file": os.path.basename(backup_path),
        "created_at": utc_now_iso(),
    }


def start_backup_scheduler(stop_event: threading.Event | None = None) -> bool:
    """Start a lightweight periodic SQLite backup thread."""
    global _backup_scheduler_started
    if _backup_scheduler_started:
        return False
    _backup_scheduler_started = True
    interval_hours = max(int(getattr(Config, "BACKUP_INTERVAL_HOURS", 24) or 24), 1)
    stop_event = stop_event or threading.Event()

    def _loop() -> None:
        record_service_status("backup_scheduler", mode=f"{interval_hours}h", status="running")
        while not stop_event.wait(interval_hours * 3600):
            try:
                result = create_sqlite_backup("scheduled")
                record_service_status("backup_scheduler", mode=f"{interval_hours}h", status="running", extra={"last_backup": result})
            except Exception as exc:
                record_service_status("backup_scheduler", mode=f"{interval_hours}h", status="error", error=str(exc))

    thread = threading.Thread(target=_loop, name="chobot-backup-scheduler", daemon=True)
    thread.start()
    return True


def list_backups(limit: int = 25) -> dict[str, Any]:
    directory = Path(backup_dir_path())
    entries = []
    if directory.exists():
        for path in sorted(directory.glob("*.db"), key=lambda item: item.stat().st_mtime, reverse=True)[:limit]:
            stat = path.stat()
            entries.append(
                {
                    "file": path.name,
                    "size_bytes": stat.st_size,
                    "created_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                }
            )
    return {
        "backend": get_backend(),
        "backup_dir": safe_backup_dir_label(),
        "entries": entries,
    }
