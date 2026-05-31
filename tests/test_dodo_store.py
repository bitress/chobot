import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

os.environ["DB_BACKEND"] = "sqlite"
os.environ["SQLITE_DB_PATH"] = os.path.join(tempfile.gettempdir(), "chobot_dodo_store_tests.db")

try:
    os.remove(os.environ["SQLITE_DB_PATH"])
except FileNotFoundError:
    pass

from utils import database  # noqa: E402
from utils.config import Config  # noqa: E402
from utils.database import connect_db  # noqa: E402
from utils.dodo_store import mark_stale_dodo_codes, persist_dodo_update, recent_dodo_captures  # noqa: E402

Config.DB_BACKEND = "sqlite"
Config.SQLITE_DB_PATH = os.environ["SQLITE_DB_PATH"]
database.get_engine.cache_clear()
database.get_session_factory.cache_clear()
database._schema_ready = False


class DodoStoreTests(unittest.TestCase):
    def test_persist_creates_local_island_when_missing(self):
        self.assertTrue(persist_dodo_update("Tala", "ABC12", channel_id=123))

        with connect_db() as db:
            row = db.execute("SELECT name, dodo_code, status, channel_id FROM islands WHERE id = ?", ("tala",)).fetchone()

        self.assertEqual(row["name"], "TALA")
        self.assertEqual(row["dodo_code"], "ABC12")
        self.assertEqual(row["status"], "ONLINE")
        self.assertEqual(row["channel_id"], "123")

    def test_persist_updates_existing_island(self):
        self.assertTrue(persist_dodo_update("Tala", "ABC12", channel_id=123))
        self.assertTrue(persist_dodo_update("Tala", "XYZ89", channel_id=456))

        with connect_db() as db:
            row = db.execute("SELECT dodo_code, channel_id FROM islands WHERE id = ?", ("tala",)).fetchone()

        self.assertEqual(row["dodo_code"], "XYZ89")
        self.assertEqual(row["channel_id"], "123")

    def test_persist_offline_clears_stale_code_and_records_capture(self):
        self.assertTrue(persist_dodo_update("Hiraya", "LMN45", channel_id=789))
        self.assertTrue(persist_dodo_update("Hiraya", status="OFFLINE", source="manual"))

        with connect_db() as db:
            row = db.execute("SELECT dodo_code, status FROM islands WHERE id = ?", ("hiraya",)).fetchone()

        self.assertIsNone(row["dodo_code"])
        self.assertEqual(row["status"], "OFFLINE")
        captures = recent_dodo_captures(limit=5)
        self.assertTrue(any(c["island_name"] == "HIRAYA" and c["status"] == "OFFLINE" for c in captures))

    def test_mark_stale_dodo_codes_clears_old_codes(self):
        self.assertTrue(persist_dodo_update("Stale", "QWERT", channel_id=555))
        old_time = (datetime.now(timezone.utc) - timedelta(minutes=180)).isoformat()
        with connect_db() as db:
            db.execute("UPDATE islands SET updated_at = ? WHERE id = ?", (old_time, "stale"))

        self.assertEqual(mark_stale_dodo_codes(120), 1)

        with connect_db() as db:
            row = db.execute("SELECT dodo_code, status FROM islands WHERE id = ?", ("stale",)).fetchone()

        self.assertIsNone(row["dodo_code"])
        self.assertEqual(row["status"], "OFFLINE")


if __name__ == "__main__":
    unittest.main()
