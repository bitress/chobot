import importlib
import threading
import time
from datetime import datetime

from utils import database
from utils.auth_tokens import make_auth_token
from utils.config import Config


class FakeDataManager:
    def __init__(self):
        self.cache = {"apple": "A", "_display": {"apple": "Apple"}}
        self.last_update = datetime.now()
        self.cache_refresh_hours = 1
        self.lock = threading.Lock()
        self.last_refresh_attempt = self.last_update
        self.last_refresh_status = "ok"
        self.last_refresh_error = None


def _load_app(monkeypatch, tmp_path):
    monkeypatch.setattr(Config, "DB_BACKEND", "sqlite")
    monkeypatch.setattr(Config, "DATABASE_URL", "")
    monkeypatch.setattr(Config, "SQLITE_DB_PATH", str(tmp_path / "routes.db"))
    monkeypatch.setattr(Config, "BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setattr(Config, "DASHBOARD_SECRET", "test-secret")
    monkeypatch.setattr(Config, "FLASK_SECRET_KEY", "test-secret")
    monkeypatch.setattr(Config, "HEALTH_CACHE_MAX_AGE_SECONDS", 7200)
    database.get_engine.cache_clear()
    database.get_session_factory.cache_clear()
    monkeypatch.setattr(database, "_schema_ready", False)

    import utils.ops_status as ops_status
    importlib.reload(ops_status)

    import api.dashboard as dashboard
    importlib.reload(dashboard)

    import api.flask_api as flask_api
    importlib.reload(flask_api)
    flask_api.set_data_manager(FakeDataManager())
    return flask_api.app


def test_public_health_route_returns_ops_shape(monkeypatch, tmp_path):
    app = _load_app(monkeypatch, tmp_path)

    response = app.test_client().get("/api/health")
    data = response.get_json()

    assert response.status_code == 200
    assert data["status"] == "ok"
    assert data["cache"]["items"] == 1
    assert "services" not in data


def test_runtime_status_requires_auth(monkeypatch, tmp_path):
    app = _load_app(monkeypatch, tmp_path)

    response = app.test_client().get("/dashboard/api/runtime-status")

    assert response.status_code == 401


def test_runtime_status_with_secret_includes_private_ops(monkeypatch, tmp_path):
    app = _load_app(monkeypatch, tmp_path)

    response = app.test_client().get(
        "/dashboard/api/runtime-status",
        headers={"Authorization": "Bearer test-secret"},
    )
    data = response.get_json()

    assert response.status_code == 200
    assert data["cache"]["items"] == 1
    assert "integrations" in data
    assert "services" in data


def test_maintenance_mode_route_updates_settings(monkeypatch, tmp_path):
    app = _load_app(monkeypatch, tmp_path)

    response = app.test_client().post(
        "/dashboard/api/maintenance-mode",
        headers={"Authorization": "Bearer test-secret"},
        json={
            "maintenance_mode": True,
            "disable_dodo_reveals": True,
            "disable_refresh": True,
            "disable_commands": True,
            "message": "Paused",
        },
    )
    data = response.get_json()

    assert response.status_code == 200
    assert data["maintenance"]["maintenance_mode"] is True
    assert data["maintenance"]["disable_dodo_reveals"] is True
    assert data["maintenance"]["disable_refresh"] is True
    assert data["maintenance"]["disable_commands"] is True


def test_public_browser_islands_hides_dodo_code(monkeypatch, tmp_path):
    app = _load_app(monkeypatch, tmp_path)
    with app.app_context():
        from api.dashboard import get_db

        db = get_db()
        try:
            db.execute(
                "INSERT INTO islands "
                "(id, name, cat, type, dodo_code, items, theme, status, visitors, description, seasonal, required_roles) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("test", "TEST", "public", "Free", "ABCDE", "[]", "teal", "ONLINE", 0, "", "", "[]"),
            )
            db.commit()
        finally:
            db.close()

    response = app.test_client().get("/api/browser/islands")
    data = response.get_json()

    assert response.status_code == 200
    assert data["items"][0]["name"] == "TEST"
    assert "dodo_code" not in data["items"][0]


def test_subscription_and_dodo_queue_public_apis(monkeypatch, tmp_path):
    app = _load_app(monkeypatch, tmp_path)
    token = make_auth_token({
        "user_id": "42",
        "username": "Tester",
        "roles": [],
        "avatar": "",
        "is_mod": False,
        "is_admin": False,
    })
    client = app.test_client()

    sub_response = client.post(
        "/api/subscriptions",
        headers={"Authorization": f"Bearer {token}"},
        json={"kind": "item", "target": "gold rose"},
    )
    queue_response = client.post(
        "/api/islands/Test/queue",
        headers={"Authorization": f"Bearer {token}"},
        json={"note": "ready"},
    )
    mine_response = client.get("/api/dodo-queue/me", headers={"Authorization": f"Bearer {token}"})

    assert sub_response.status_code == 200
    assert queue_response.status_code == 200
    assert queue_response.get_json()["status"] == "waiting"
    assert mine_response.get_json()["items"][0]["island_name"] == "TEST"


def test_admin_incidents_and_command_analytics(monkeypatch, tmp_path):
    app = _load_app(monkeypatch, tmp_path)
    now = int(time.time())
    with app.app_context():
        from api.dashboard import get_db

        db = get_db()
        try:
            db.execute(
                "INSERT INTO command_search_events "
                "(command, query, normalized_query, source, found, result_count, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("find", "gold rose", "gold rose", "api", 0, 0, now),
            )
            db.execute(
                "INSERT INTO island_visits (ign, origin_island, destination, authorized, timestamp, island_type, has_island_access) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("Player", "Home", "TEST", 0, now, "sub", 0),
            )
            db.commit()
        finally:
            db.close()

    client = app.test_client()
    headers = {"Authorization": "Bearer test-secret"}
    incidents = client.get("/dashboard/api/incidents", headers=headers)
    analytics = client.get("/dashboard/api/command-analytics?days=3650", headers=headers)

    assert incidents.status_code == 200
    assert incidents.get_json()["summary"]["unknown_travelers"] == 1
    assert analytics.status_code == 200
    assert analytics.get_json()["summary"]["total_searches"] == 1


def test_search_aliases_api(monkeypatch, tmp_path):
    app = _load_app(monkeypatch, tmp_path)
    client = app.test_client()
    headers = {"Authorization": "Bearer test-secret"}

    response = client.post(
        "/dashboard/api/search-aliases",
        headers=headers,
        json={"kind": "item", "alias": "froggy chair typo", "target": "froggy chair"},
    )
    listing = client.get("/dashboard/api/search-aliases", headers=headers)

    assert response.status_code == 200
    assert listing.get_json()["items"][0]["target"] == "froggy chair"
