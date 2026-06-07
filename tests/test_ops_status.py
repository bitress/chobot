import importlib
import threading
from datetime import datetime, timedelta

from utils import database, ops_status
from utils.config import Config


class FakeDataManager:
    def __init__(self, cache, last_update):
        self.cache = cache
        self.last_update = last_update
        self.cache_refresh_hours = 1
        self.lock = threading.Lock()
        self.last_refresh_attempt = last_update
        self.last_refresh_status = "ok"
        self.last_refresh_error = None


def _reset_database(monkeypatch, tmp_path):
    db_path = tmp_path / "ops-test.db"
    monkeypatch.setattr(Config, "DB_BACKEND", "sqlite")
    monkeypatch.setattr(Config, "DATABASE_URL", "")
    monkeypatch.setattr(Config, "SQLITE_DB_PATH", str(db_path))
    monkeypatch.setattr(Config, "BACKUP_DIR", str(tmp_path / "backups"))
    database.get_engine.cache_clear()
    database.get_session_factory.cache_clear()
    monkeypatch.setattr(database, "_schema_ready", False)
    importlib.reload(ops_status)
    return db_path


def test_build_health_payload_reports_ok_for_fresh_cache(monkeypatch, tmp_path):
    _reset_database(monkeypatch, tmp_path)
    dm = FakeDataManager({"apple": "A", "_display": {"apple": "Apple"}}, datetime.now() - timedelta(seconds=30))

    payload = ops_status.build_health_payload(data_manager=dm)

    assert payload["status"] == "ok"
    assert payload["cache"]["items"] == 1
    assert payload["database"]["status"] == "ok"


def test_build_health_payload_marks_stale_cache_degraded(monkeypatch, tmp_path):
    _reset_database(monkeypatch, tmp_path)
    monkeypatch.setattr(Config, "HEALTH_CACHE_MAX_AGE_SECONDS", 10)
    dm = FakeDataManager({"apple": "A"}, datetime.now() - timedelta(seconds=60))

    payload = ops_status.build_health_payload(data_manager=dm)

    assert payload["status"] == "degraded"
    assert "item cache is stale" in payload["reasons"]


def test_maintenance_settings_round_trip(monkeypatch, tmp_path):
    _reset_database(monkeypatch, tmp_path)

    saved = ops_status.update_maintenance_settings({
        "maintenance_mode": True,
        "disable_dodo_reveals": True,
        "disable_refresh": False,
        "message": "Paused for maintenance",
    })

    assert saved == {
        "maintenance_mode": True,
        "disable_dodo_reveals": True,
        "disable_refresh": False,
        "disable_commands": False,
        "islands": {},
        "message": "Paused for maintenance",
    }
    assert ops_status.get_maintenance_settings() == saved


def test_sqlite_backup_creation_and_listing(monkeypatch, tmp_path):
    db_path = _reset_database(monkeypatch, tmp_path)
    db_path.write_bytes(b"sqlite bytes")

    backup = ops_status.create_sqlite_backup("test")
    listing = ops_status.list_backups()

    assert backup["ok"] is True
    assert backup["file"].endswith(".db")
    assert listing["entries"][0]["file"] == backup["file"]
    assert listing["entries"][0]["size_bytes"] == len(b"sqlite bytes")
