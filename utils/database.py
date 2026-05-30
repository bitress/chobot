"""SQLAlchemy-backed database compatibility helpers.

The app historically used sqlite3/aiosqlite directly.  This module keeps the
small DB-API surface the rest of the code expects while allowing the backend to
be selected with configuration.
"""

from __future__ import annotations

import asyncio
import os
import re
import threading
import time
from functools import lru_cache
from typing import Any, Iterable
from urllib.parse import quote_plus

from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from utils.config import Config


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_SQLITE_PATH = os.path.join(PROJECT_ROOT, "chobot.db")
_schema_lock = threading.Lock()
_schema_ready = False


DbError = SQLAlchemyError
DbOperationalError = Exception


class Row:
    def __init__(self, values: Iterable[Any], columns: Iterable[str] | None = None):
        self._values = tuple(values)
        self._columns = list(columns or [])
        self._index = {name: idx for idx, name in enumerate(self._columns)}

    def __getitem__(self, key):
        if isinstance(key, str):
            return self._values[self._index[key]]
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def keys(self):
        return self._columns

    def items(self):
        return ((key, self[key]) for key in self._columns)

    def get(self, key: str, default: Any = None) -> Any:
        return self[key] if key in self._index else default


class Cursor:
    def __init__(self, cursor, rowcount_override: int | None = None):
        self._cursor = cursor
        self._rowcount_override = rowcount_override

    @property
    def rowcount(self):
        if self._rowcount_override is not None:
            return self._rowcount_override
        return self._cursor.rowcount

    @property
    def lastrowid(self):
        return getattr(self._cursor, "lastrowid", None)

    def fetchone(self):
        row = self._cursor.fetchone()
        if row is None:
            return None
        return Row(row, self._columns())

    def fetchall(self):
        columns = self._columns()
        return [Row(row, columns) for row in self._cursor.fetchall()]

    def _columns(self):
        return [col[0] for col in self._cursor.description or []]


class Connection:
    def __init__(self, raw_conn, dialect: str):
        self._conn = raw_conn
        self._dialect = dialect
        self._last_rowcount = 0
        self.row_factory = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type:
            self.rollback()
        else:
            self.commit()
        self.close()
        return False

    def execute(self, sql: str, params: Iterable[Any] | None = None):
        if self._dialect == "mysql" and _is_select_changes(sql):
            return StaticCursor([(self._last_rowcount,)], ["changes()"])

        sql, params = _adapt_sql(sql, params or (), self._dialect)
        cur = self._conn.cursor()
        cur.execute(sql, tuple(params or ()))
        self._last_rowcount = cur.rowcount
        return Cursor(cur)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


class StaticCursor:
    def __init__(self, rows: list[tuple], columns: list[str]):
        self._rows = rows
        self._columns = columns
        self.rowcount = len(rows)
        self.lastrowid = None

    def fetchone(self):
        if not self._rows:
            return None
        return Row(self._rows.pop(0), self._columns)

    def fetchall(self):
        rows = self._rows
        self._rows = []
        return [Row(row, self._columns) for row in rows]


class AsyncCursor:
    def __init__(self, cursor: Cursor):
        self._cursor = cursor

    @property
    def rowcount(self):
        return self._cursor.rowcount

    @property
    def lastrowid(self):
        return self._cursor.lastrowid

    async def fetchone(self):
        return await asyncio.to_thread(self._cursor.fetchone)

    async def fetchall(self):
        return await asyncio.to_thread(self._cursor.fetchall)


class AsyncConnection:
    def __init__(self, conn: Connection):
        self._conn = conn

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type:
            await self.rollback()
        else:
            await self.commit()
        await self.close()
        return False

    async def execute(self, sql: str, params: Iterable[Any] | None = None):
        cursor = await asyncio.to_thread(self._conn.execute, sql, params)
        return AsyncCursor(cursor)

    async def commit(self):
        await asyncio.to_thread(self._conn.commit)

    async def rollback(self):
        await asyncio.to_thread(self._conn.rollback)

    async def close(self):
        await asyncio.to_thread(self._conn.close)


def get_backend() -> str:
    backend = (Config.DB_BACKEND or "sqlite").strip().lower()
    if backend in {"mysql", "mariadb"}:
        return "mysql"
    return "sqlite"


def get_database_url() -> str:
    if Config.DATABASE_URL:
        return Config.DATABASE_URL
    if get_backend() == "mysql":
        missing = [
            name for name, value in {
                "MYSQL_HOST": Config.MYSQL_HOST,
                "MYSQL_USER": Config.MYSQL_USER,
                "MYSQL_DATABASE": Config.MYSQL_DATABASE,
            }.items()
            if not value
        ]
        if missing:
            raise RuntimeError(f"Missing MySQL database setting(s): {', '.join(missing)}")
        user = quote_plus(Config.MYSQL_USER)
        password = quote_plus(Config.MYSQL_PASSWORD)
        host = Config.MYSQL_HOST
        return (
            f"mysql+pymysql://{user}:{password}"
            f"@{host}:{Config.MYSQL_PORT}/{Config.MYSQL_DATABASE}"
            "?charset=utf8mb4"
        )
    sqlite_path = Config.SQLITE_DB_PATH or DEFAULT_SQLITE_PATH
    return f"sqlite:///{sqlite_path.replace(os.sep, '/')}"


@lru_cache(maxsize=1)
def get_engine():
    kwargs = {
        "pool_pre_ping": True,
        "pool_recycle": 1800,
        "future": True,
    }
    if get_backend() == "sqlite":
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(get_database_url(), **kwargs)


@lru_cache(maxsize=1)
def get_session_factory():
    return sessionmaker(bind=get_engine(), autoflush=False, autocommit=False, future=True)


def ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        from utils.db_models import Base

        Base.metadata.create_all(get_engine())
        _ensure_tenant_foundation()
        _schema_ready = True


def connect_db(*_, **__) -> Connection:
    ensure_schema()
    return Connection(get_engine().raw_connection(), get_backend())


def connect_async_db(*_, **__) -> AsyncConnection:
    return AsyncConnection(connect_db())


def get_default_tenant_id() -> str:
    """Return the tenant used by legacy single-community code paths."""
    return Config.DEFAULT_TENANT_ID


def _execute_bootstrap(raw_conn, dialect: str, sql: str, params: Iterable[Any] | None = None):
    adapted_sql, adapted_params = _adapt_sql(sql, params or (), dialect)
    cur = raw_conn.cursor()
    cur.execute(adapted_sql, tuple(adapted_params or ()))
    return cur


def _try_bootstrap(raw_conn, dialect: str, sql: str, params: Iterable[Any] | None = None) -> None:
    try:
        _execute_bootstrap(raw_conn, dialect, sql, params)
    except Exception:
        # Bootstrap DDL is intentionally idempotent across SQLite/MySQL versions.
        # Existing columns/indexes can raise dialect-specific errors; those are safe.
        pass


def _ensure_tenant_column(raw_conn, dialect: str, table_name: str, default_tenant_id: str) -> None:
    quoted_table = f"`{table_name}`" if dialect == "mysql" else table_name
    _try_bootstrap(
        raw_conn,
        dialect,
        f"ALTER TABLE {quoted_table} ADD COLUMN tenant_id VARCHAR(64) NOT NULL DEFAULT '{default_tenant_id}'",
    )
    _try_bootstrap(
        raw_conn,
        dialect,
        f"UPDATE {quoted_table} SET tenant_id = ? WHERE tenant_id IS NULL OR tenant_id = ''",
        (default_tenant_id,),
    )
    _try_bootstrap(
        raw_conn,
        dialect,
        f"CREATE INDEX ix_{table_name}_tenant_id ON {quoted_table} (tenant_id)",
    )


def _seed_default_tenant(raw_conn, dialect: str) -> None:
    now = int(time.time())
    tenant_id = Config.DEFAULT_TENANT_ID
    tenant_name = Config.DEFAULT_TENANT_NAME
    tenant_slug = Config.DEFAULT_TENANT_SLUG

    _execute_bootstrap(
        raw_conn,
        dialect,
        """
        INSERT OR IGNORE INTO tenants (id, name, slug, status, plan, created_at, updated_at)
        VALUES (?, ?, ?, 'active', 'legacy', ?, ?)
        """,
        (tenant_id, tenant_name, tenant_slug, now, now),
    )
    _execute_bootstrap(
        raw_conn,
        dialect,
        "UPDATE tenants SET name = ?, slug = ?, updated_at = ? WHERE id = ?",
        (tenant_name, tenant_slug, now, tenant_id),
    )

    _execute_bootstrap(
        raw_conn,
        dialect,
        """
        INSERT OR IGNORE INTO tenant_discord_configs (
            tenant_id, guild_id, member_category_id, free_category_id, log_channel_id,
            flight_listen_channel_id, free_flight_listen_channel_id, flight_log_channel_id,
            mod_role_id, island_access_role_id, bot_enabled, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
        """,
        (
            tenant_id,
            str(Config.GUILD_ID or ""),
            str(Config.CATEGORY_ID or ""),
            str(Config.FREE_CATEGORY_ID or ""),
            str(Config.LOG_CHANNEL_ID or ""),
            str(Config.FLIGHT_LISTEN_CHANNEL_ID or ""),
            str(Config.FREE_ISLAND_FLIGHT_LISTEN_CHANNEL_ID or ""),
            str(Config.FLIGHT_LOG_CHANNEL_ID or ""),
            str(Config.ADMIN_ROLE_ID or ""),
            str(Config.ISLAND_ACCESS_ROLE or ""),
            now,
        ),
    )
    _execute_bootstrap(
        raw_conn,
        dialect,
        """
        UPDATE tenant_discord_configs
        SET guild_id = ?, member_category_id = ?, free_category_id = ?, log_channel_id = ?,
            flight_listen_channel_id = ?, free_flight_listen_channel_id = ?,
            flight_log_channel_id = ?, mod_role_id = ?, island_access_role_id = ?,
            updated_at = ?
        WHERE tenant_id = ?
        """,
        (
            str(Config.GUILD_ID or ""),
            str(Config.CATEGORY_ID or ""),
            str(Config.FREE_CATEGORY_ID or ""),
            str(Config.LOG_CHANNEL_ID or ""),
            str(Config.FLIGHT_LISTEN_CHANNEL_ID or ""),
            str(Config.FREE_ISLAND_FLIGHT_LISTEN_CHANNEL_ID or ""),
            str(Config.FLIGHT_LOG_CHANNEL_ID or ""),
            str(Config.ADMIN_ROLE_ID or ""),
            str(Config.ISLAND_ACCESS_ROLE or ""),
            now,
            tenant_id,
        ),
    )

    _execute_bootstrap(
        raw_conn,
        dialect,
        """
        INSERT OR IGNORE INTO tenant_twitch_configs (tenant_id, channel_name, bot_enabled, updated_at)
        VALUES (?, ?, 1, ?)
        """,
        (tenant_id, Config.TWITCH_CHANNEL or "", now),
    )
    _execute_bootstrap(
        raw_conn,
        dialect,
        "UPDATE tenant_twitch_configs SET channel_name = ?, updated_at = ? WHERE tenant_id = ?",
        (Config.TWITCH_CHANNEL or "", now, tenant_id),
    )


def _ensure_tenant_foundation() -> None:
    raw_conn = get_engine().raw_connection()
    dialect = get_backend()
    try:
        default_tenant_id = Config.DEFAULT_TENANT_ID.replace("'", "''")
        for table_name in (
            "command_claims",
            "island_subscriptions",
            "settings",
            "islands",
            "island_bot_status",
            "island_metadata",
            "island_visits",
            "warnings",
            "dodo_reveal_messages",
        ):
            _ensure_tenant_column(raw_conn, dialect, table_name, default_tenant_id)
        _seed_default_tenant(raw_conn, dialect)
        raw_conn.commit()
    except Exception:
        raw_conn.rollback()
        raise
    finally:
        raw_conn.close()


def _is_select_changes(sql: str) -> bool:
    return re.sub(r"\s+", " ", sql.strip()).lower() == "select changes()"


def _adapt_sql(sql: str, params: Iterable[Any], dialect: str):
    if dialect != "mysql":
        return sql, params

    adapted = sql
    adapted = _adapt_mysql_ddl(adapted)
    adapted = _adapt_mysql_datetime_functions(adapted)
    adapted = _adapt_mysql_upsert(adapted)
    adapted = adapted.replace("INSERT OR IGNORE INTO", "INSERT IGNORE INTO")
    adapted = re.sub(r"\browid\b", "id", adapted, flags=re.I)
    adapted = re.sub(r'"([A-Za-z_][A-Za-z0-9_]*)"', r"`\1`", adapted)
    adapted = _quote_settings_key(adapted)
    adapted = _replace_qmarks(adapted)
    return adapted, params


def _replace_qmarks(sql: str) -> str:
    out = []
    in_single = False
    in_double = False
    for ch in sql:
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        if ch == "?" and not in_single and not in_double:
            out.append("%s")
        else:
            out.append(ch)
    return "".join(out)


def _adapt_mysql_ddl(sql: str) -> str:
    if not re.search(r"CREATE\s+TABLE", sql, re.I):
        return sql

    replacements = [
        (r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b", "BIGINT PRIMARY KEY AUTO_INCREMENT"),
        (r"\bINTEGER\s+PRIMARY\s+KEY\b", "BIGINT PRIMARY KEY"),
        (r"\bid\s+TEXT\s+PRIMARY\s+KEY\b", "id VARCHAR(255) PRIMARY KEY"),
        (r"\bname\s+TEXT\s+PRIMARY\s+KEY\b", "name VARCHAR(255) PRIMARY KEY"),
        (r"\bkey\s+TEXT\s+PRIMARY\s+KEY\b", "`key` VARCHAR(255) PRIMARY KEY"),
        (r"\bisland_id\s+TEXT\s+PRIMARY\s+KEY\b", "island_id VARCHAR(255) PRIMARY KEY"),
        (r"\bisland_clean\s+TEXT\s+NOT\s+NULL\b", "island_clean VARCHAR(255) NOT NULL"),
        (r"\bkind\s+TEXT\s+NOT\s+NULL\b", "kind VARCHAR(64) NOT NULL"),
    ]
    for pattern, replacement in replacements:
        sql = re.sub(pattern, replacement, sql, flags=re.I)
    if re.search(r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+warnings\b", sql, re.I) and not re.search(r"\bid\b", sql, re.I):
        sql = re.sub(r"\(\s*", "(\n                id BIGINT PRIMARY KEY AUTO_INCREMENT,\n                ", sql, count=1)
    return sql


def _adapt_mysql_datetime_functions(sql: str) -> str:
    sql = re.sub(
        r"strftime\('%s','now','\+8 hours','start of day','-8 hours'\)",
        "UNIX_TIMESTAMP(DATE_SUB(DATE(UTC_TIMESTAMP() + INTERVAL 8 HOUR), INTERVAL 8 HOUR))",
        sql,
        flags=re.I,
    )
    sql = re.sub(
        r"strftime\('%s','now','-(\d+) days'\)",
        r"UNIX_TIMESTAMP(DATE_SUB(UTC_TIMESTAMP(), INTERVAL \1 DAY))",
        sql,
        flags=re.I,
    )
    sql = re.sub(
        r"DATE\(timestamp,\s*'unixepoch',\s*'\+8 hours'\)",
        "DATE(FROM_UNIXTIME(timestamp) + INTERVAL 8 HOUR)",
        sql,
        flags=re.I,
    )
    sql = re.sub(
        r"datetime\(timestamp,\s*'unixepoch',\s*'\+8 hours'\)",
        "DATE_FORMAT(FROM_UNIXTIME(timestamp) + INTERVAL 8 HOUR, '%Y-%m-%d %H:%i:%s')",
        sql,
        flags=re.I,
    )
    sql = re.sub(
        r"CAST\(strftime\('%H',\s*timestamp,\s*'unixepoch',\s*'\+8 hours'\)\s+AS\s+INTEGER\)",
        "CAST(DATE_FORMAT(FROM_UNIXTIME(timestamp) + INTERVAL 8 HOUR, '%H') AS UNSIGNED)",
        sql,
        flags=re.I,
    )
    sql = re.sub(
        r"CAST\(strftime\('%w',\s*timestamp,\s*'unixepoch',\s*'\+8 hours'\)\s+AS\s+INTEGER\)",
        "(DAYOFWEEK(FROM_UNIXTIME(timestamp) + INTERVAL 8 HOUR) - 1)",
        sql,
        flags=re.I,
    )
    return sql


def _adapt_mysql_upsert(sql: str) -> str:
    marker = re.search(r"\s+ON\s+CONFLICT\s*\(([^)]+)\)\s+DO\s+UPDATE\s+SET\s+", sql, re.I)
    if not marker:
        return sql

    before = sql[: marker.start()]
    assignments = sql[marker.end():]
    assignments = re.sub(r"\bexcluded\.([A-Za-z_][A-Za-z0-9_]*)", r"VALUES(\1)", assignments)
    return f"{before} ON DUPLICATE KEY UPDATE {assignments}"


def _quote_settings_key(sql: str) -> str:
    if not re.search(r"\b(?:settings|tenant_settings)\b", sql, re.I):
        return sql
    sql = re.sub(r"\bWHERE\s+key\s*=", "WHERE `key` =", sql, flags=re.I)
    sql = re.sub(r"\bON\s+CONFLICT\s*\(\s*key\s*\)", "ON CONFLICT(`key`)", sql, flags=re.I)
    sql = re.sub(r"\bON\s+CONFLICT\s*\(\s*tenant_id\s*,\s*key\s*\)", "ON CONFLICT(tenant_id, `key`)", sql, flags=re.I)
    sql = re.sub(r"\(\s*key\s*,", "(`key`,", sql, flags=re.I)
    sql = re.sub(r",\s*key\s*,", ", `key`,", sql, flags=re.I)
    sql = re.sub(r"SELECT\s+key\s*,", "SELECT `key`,", sql, flags=re.I)
    return sql
