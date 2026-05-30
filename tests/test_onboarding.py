import os
import tempfile
import unittest

os.environ["DB_BACKEND"] = "sqlite"
os.environ["SQLITE_DB_PATH"] = os.path.join(tempfile.gettempdir(), "chobot_onboarding_tests.db")
os.environ["DASHBOARD_SECRET"] = "testsecret"

try:
    os.remove(os.environ["SQLITE_DB_PATH"])
except FileNotFoundError:
    pass

from api.flask_api import app  # noqa: E402
from utils.database import connect_db  # noqa: E402
from utils.database import get_default_tenant_id  # noqa: E402
from utils.tenant_config import load_tenant_runtime_config  # noqa: E402


class OnboardingApiTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        self.headers = {"Authorization": "Bearer testsecret"}

    def test_complete_onboarding_without_twitch(self):
        payload = {
            "tenant": {"name": "No Twitch", "slug": "no-twitch", "plan": "trial"},
            "branding": {"logo_url": "https://example.com/logo.png", "theme_color": "pink"},
            "discord": {"guild_id": "123", "free_category_id": "10"},
            "twitch": {},
            "islands": [{"id": "tala", "name": "TALA", "cat": "public"}],
        }
        resp = self.client.post("/dashboard/api/onboarding/complete", json=payload, headers=self.headers)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["tenant_id"], "no-twitch")

        db = connect_db()
        try:
            twitch = db.execute(
                "SELECT * FROM tenant_twitch_configs WHERE tenant_id = ?",
                ("no-twitch",),
            ).fetchall()
            settings = {
                row["key"]: row["value"]
                for row in db.execute(
                    "SELECT key, value FROM tenant_settings WHERE tenant_id = ?",
                    ("no-twitch",),
                ).fetchall()
            }
        finally:
            db.close()

        self.assertEqual(twitch, [])
        self.assertEqual(settings["brand.logo_url"], "https://example.com/logo.png")
        self.assertIn("onboarding.completed_at", settings)

    def test_complete_onboarding_with_twitch(self):
        payload = {
            "tenant": {"name": "With Twitch", "slug": "with-twitch", "plan": "starter"},
            "branding": {"theme_color": "teal"},
            "discord": {"guild_id": "456"},
            "twitch": {"channel_name": "treasuretv", "bot_enabled": True},
            "islands": [],
        }
        resp = self.client.post("/dashboard/api/onboarding/complete", json=payload, headers=self.headers)

        self.assertEqual(resp.status_code, 200)

        db = connect_db()
        try:
            twitch = db.execute(
                "SELECT channel_name, bot_enabled FROM tenant_twitch_configs WHERE tenant_id = ?",
                ("with-twitch",),
            ).fetchone()
        finally:
            db.close()

        self.assertEqual(twitch["channel_name"], "treasuretv")
        self.assertEqual(twitch["bot_enabled"], 1)

    def test_non_admin_session_cannot_complete_onboarding(self):
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["mod_logged_in"] = True
            sess["mod_role"] = "viewer"

        resp = client.post(
            "/dashboard/api/onboarding/complete",
            json={
                "tenant": {"name": "Blocked", "slug": "blocked"},
                "discord": {"guild_id": "789"},
            },
        )

        self.assertEqual(resp.status_code, 403)

    def test_two_tenants_can_use_same_island_slug(self):
        for slug, description in (("tenant-a", "A"), ("tenant-b", "B")):
            resp = self.client.post(
                "/dashboard/api/onboarding/complete",
                json={
                    "tenant": {"name": slug, "slug": slug},
                    "discord": {"guild_id": slug},
                    "islands": [{"id": "tala", "name": "TALA", "description": description}],
                },
                headers=self.headers,
            )
            self.assertEqual(resp.status_code, 200)

        db = connect_db()
        try:
            rows = db.execute(
                "SELECT id, tenant_id, description FROM islands WHERE tenant_id IN (?, ?) ORDER BY tenant_id",
                ("tenant-a", "tenant-b"),
            ).fetchall()
        finally:
            db.close()

        self.assertEqual(
            [(row["id"], row["tenant_id"], row["description"]) for row in rows],
            [("tenant-a:tala", "tenant-a", "A"), ("tenant-b:tala", "tenant-b", "B")],
        )

    def test_discord_scan_accepts_channel_payload(self):
        resp = self.client.post(
            "/dashboard/api/onboarding/discord-scan",
            json={
                "guild_id": "1",
                "free_category_id": "10",
                "member_category_id": "20",
                "channels": [
                    {"id": "101", "name": "tala", "type": 0, "parent_id": "10"},
                    {"id": "201", "name": "hiraya", "type": 0, "parent_id": "20"},
                    {"id": "301", "name": "ignore-me", "type": 0, "parent_id": "30"},
                ],
            },
            headers=self.headers,
        )

        self.assertEqual(resp.status_code, 200)
        islands = resp.get_json()["islands"]
        self.assertEqual([island["id"] for island in islands], ["tala", "hiraya"])
        self.assertEqual([island["cat"] for island in islands], ["public", "member"])

    def test_runtime_config_loads_customer_tenant_settings(self):
        resp = self.client.post(
            "/dashboard/api/onboarding/complete",
            json={
                "tenant": {"name": "Runtime Tenant", "slug": "runtime-tenant", "plan": "growth"},
                "branding": {"logo_url": "https://example.com/runtime.png", "theme_color": "mint"},
                "discord": {
                    "guild_id": "321",
                    "member_category_id": "654",
                    "free_category_id": "987",
                    "island_access_role_id": "111",
                },
                "twitch": {"channel_name": "runtime_tv", "bot_enabled": True},
                "islands": [
                    {"id": "tala", "name": "TALA", "cat": "public"},
                    {"id": "hiraya", "name": "HIRAYA", "cat": "member"},
                ],
            },
            headers=self.headers,
        )
        self.assertEqual(resp.status_code, 200)

        config = load_tenant_runtime_config("runtime-tenant")

        self.assertEqual(config.name, "Runtime Tenant")
        self.assertEqual(config.plan, "growth")
        self.assertEqual(config.guild_id, 321)
        self.assertEqual(config.member_category_id, 654)
        self.assertEqual(config.free_category_id, 987)
        self.assertEqual(config.island_access_role_id, 111)
        self.assertTrue(config.twitch_bot_enabled)
        self.assertEqual(config.twitch_channel, "runtime_tv")
        self.assertEqual(config.logo_url, "https://example.com/runtime.png")
        self.assertEqual(config.theme_color, "mint")
        self.assertEqual(config.free_islands, ["TALA"])
        self.assertEqual(config.member_islands, ["HIRAYA"])

    def test_runtime_config_keeps_default_tenant_fallback_islands(self):
        config = load_tenant_runtime_config(get_default_tenant_id())

        self.assertEqual(config.tenant_id, get_default_tenant_id())
        self.assertIn("Tala", config.free_islands)
        self.assertIn("Hiraya", config.member_islands)


if __name__ == "__main__":
    unittest.main()
