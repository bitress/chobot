"""
Database configuration and session management.

Supports SQLite (default), PostgreSQL, and MySQL/MariaDB via DATABASE_URL.

Set DATABASE_URL in your .env file to choose a database backend:
  SQLite   (default): sqlite:///chobot.db
  PostgreSQL:         postgresql+psycopg2://user:pass@localhost/chobot
  MySQL/MariaDB:      mysql+pymysql://user:pass@localhost/chobot

Migrations are managed by Flask-Migrate (Alembic).  Run:
  flask db init     # first time only – creates the migrations/ folder
  flask db migrate  # auto-generate a migration from model changes
  flask db upgrade  # apply pending migrations
"""

import os
import re
import time
import logging
from collections.abc import Mapping
from datetime import datetime, timezone, timedelta

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from flask_sqlalchemy import SQLAlchemy

logger = logging.getLogger("Database")


# ---------------------------------------------------------------------------
# ORM base — shared by both Flask-SQLAlchemy and standalone usage
# ---------------------------------------------------------------------------

class Base(DeclarativeBase):
    pass


# Flask-SQLAlchemy instance configured with our custom Base.
# Call ``db.init_app(app)`` in the Flask factory before first use.
db = SQLAlchemy(model_class=Base)


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def _default_db_path() -> str:
    """Return the absolute path to the default SQLite database file."""
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "chobot.db",
    )


def get_db_url() -> str:
    """Return the synchronous database URL from config (defaulting to SQLite)."""
    from utils.config import Config
    url = getattr(Config, "DATABASE_URL", None) or ""
    if not url:
        url = f"sqlite:///{_default_db_path()}"
    return url


def get_async_db_url() -> str:
    """Return the async-compatible database URL for the Discord bot."""
    url = get_db_url()
    # sqlite:///path → sqlite+aiosqlite:///path
    if url.startswith("sqlite:///"):
        return "sqlite+aiosqlite:///" + url[len("sqlite:///"):]
    # postgresql://... or postgresql+psycopg2://... → postgresql+asyncpg://...
    if url.startswith("postgresql+psycopg2://"):
        return "postgresql+asyncpg://" + url[len("postgresql+psycopg2://"):]
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://"):]
    # mysql://... or mysql+pymysql://... → mysql+aiomysql://...
    if url.startswith("mysql+pymysql://"):
        return "mysql+aiomysql://" + url[len("mysql+pymysql://"):]
    if url.startswith("mysql://"):
        return "mysql+aiomysql://" + url[len("mysql://"):]
    return url


# ---------------------------------------------------------------------------
# Sync engine (used by the Flask dashboard outside of Flask-SQLAlchemy)
# ---------------------------------------------------------------------------

_engine = None


def get_engine():
    """Return the singleton synchronous SQLAlchemy engine."""
    global _engine
    if _engine is None:
        url = get_db_url()
        kwargs: dict = {}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
        _engine = create_engine(url, **kwargs)
        safe_url = re.sub(r":[^@/]+@", ":***@", url)
        logger.info("Database engine initialised: %s", safe_url)
    return _engine


def get_dialect_name() -> str:
    """Return the lowercase database dialect name ('sqlite', 'postgresql', 'mysql', …)."""
    return get_engine().dialect.name


# ---------------------------------------------------------------------------
# Dialect-specific SQL expression helpers
# ---------------------------------------------------------------------------

def date_utc8_expr() -> str:
    """SQL expression to convert a Unix timestamp column to a UTC+8 date string."""
    d = get_dialect_name()
    if d == "postgresql":
        return "TO_CHAR(TO_TIMESTAMP(timestamp) AT TIME ZONE 'Asia/Manila', 'YYYY-MM-DD')"
    if d in ("mysql", "mariadb"):
        return "DATE(CONVERT_TZ(FROM_UNIXTIME(timestamp), 'UTC', 'Asia/Manila'))"
    # SQLite (default)
    return "DATE(timestamp, 'unixepoch', '+8 hours')"


def hour_utc8_expr() -> str:
    """SQL expression to extract the UTC+8 hour (0-23) from a Unix timestamp column."""
    d = get_dialect_name()
    if d == "postgresql":
        return "EXTRACT(HOUR FROM TO_TIMESTAMP(timestamp) AT TIME ZONE 'Asia/Manila')::INTEGER"
    if d in ("mysql", "mariadb"):
        return "HOUR(CONVERT_TZ(FROM_UNIXTIME(timestamp), 'UTC', 'Asia/Manila'))"
    # SQLite (default)
    return "CAST(strftime('%H', timestamp, 'unixepoch', '+8 hours') AS INTEGER)"


def dow_utc8_expr() -> str:
    """SQL expression for UTC+8 day-of-week (0=Sunday … 6=Saturday)."""
    d = get_dialect_name()
    if d == "postgresql":
        return "EXTRACT(DOW FROM TO_TIMESTAMP(timestamp) AT TIME ZONE 'Asia/Manila')::INTEGER"
    if d in ("mysql", "mariadb"):
        return "(DAYOFWEEK(CONVERT_TZ(FROM_UNIXTIME(timestamp), 'UTC', 'Asia/Manila')) - 1)"
    # SQLite (default)
    return "CAST(strftime('%w', timestamp, 'unixepoch', '+8 hours') AS INTEGER)"


# ---------------------------------------------------------------------------
# Python-side timestamp helpers (database-agnostic)
# ---------------------------------------------------------------------------

def now_minus_days(days: int) -> int:
    """Return Unix timestamp for *days* days ago (computed in Python)."""
    return int(time.time()) - days * 24 * 3600


def today_start_utc8() -> int:
    """Return Unix timestamp for midnight today in UTC+8."""
    now = datetime.now(timezone.utc)
    utc8_offset = timedelta(hours=8)
    utc8_midnight = (now + utc8_offset).replace(hour=0, minute=0, second=0, microsecond=0)
    return int((utc8_midnight - utc8_offset).timestamp())


# ---------------------------------------------------------------------------
# Row / Result wrappers (make SQLAlchemy rows behave like sqlite3.Row)
# ---------------------------------------------------------------------------

class _Row(Mapping):
    """Wrap a SQLAlchemy Row to support dict-like and index access (like sqlite3.Row)."""

    __slots__ = ("_data", "_keys_list")

    def __init__(self, row):
        self._data = dict(row._mapping)
        self._keys_list = list(row._mapping.keys())

    # Mapping protocol
    def __getitem__(self, key):
        if isinstance(key, int):
            return self._data[self._keys_list[key]]
        return self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __len__(self):
        return len(self._data)

    def get(self, key, default=None):
        return self._data.get(key, default)

    def keys(self):
        return self._data.keys()

    def values(self):
        return self._data.values()

    def items(self):
        return self._data.items()


class _Result:
    """Wrap a SQLAlchemy CursorResult to expose fetchone/fetchall returning _Row objects."""

    def __init__(self, result):
        self._result = result
        self.rowcount: int = getattr(result, "rowcount", -1)
        self.lastrowid = getattr(result, "lastrowid", None)

    def fetchone(self):
        row = self._result.fetchone()
        return _Row(row) if row is not None else None

    def fetchall(self):
        return [_Row(r) for r in self._result.fetchall()]


# ---------------------------------------------------------------------------
# Parameter conversion helper
# ---------------------------------------------------------------------------

def _to_named_params(sql: str, params):
    """Convert positional ``?`` placeholders to ``:pN`` named params for SQLAlchemy text().

    Returns ``(new_sql, param_dict)``.
    """
    if not params:
        return sql, {}
    params = list(params)
    parts = sql.split("?")
    if len(parts) != len(params) + 1:
        raise ValueError(
            f"SQL placeholder count mismatch: "
            f"expected {len(parts) - 1} params, got {len(params)}"
        )
    named: dict = {}
    new_parts = [parts[0]]
    for i, (val, part) in enumerate(zip(params, parts[1:])):
        key = f"p{i}"
        named[key] = val
        new_parts.append(f":{key}")
        new_parts.append(part)
    return "".join(new_parts), named


# ---------------------------------------------------------------------------
# Synchronous connection wrapper
# ---------------------------------------------------------------------------

class DBConnection:
    """Thin wrapper around a SQLAlchemy connection that mimics the sqlite3 interface.

    Usage (mirrors existing sqlite3 pattern)::

        db = get_db()
        try:
            rows = db.execute("SELECT * FROM islands WHERE id = ?", (island_id,)).fetchall()
            ...
            db.commit()
        except Exception:
            ...
        finally:
            db.close()
    """

    def __init__(self, conn):
        self._conn = conn
        self.dialect: str = conn.dialect.name

    def execute(self, sql: str, params=None) -> _Result:
        named_sql, named_params = _to_named_params(sql, params)
        result = self._conn.execute(text(named_sql), named_params)
        return _Result(result)

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def get_db() -> DBConnection:
    """Return a new synchronous database connection (caller is responsible for closing)."""
    conn = get_engine().connect()
    return DBConnection(conn)


# ---------------------------------------------------------------------------
# Upsert helpers (database-portable)
# ---------------------------------------------------------------------------

def build_upsert_sql(table: str, id_col: str, columns: list) -> str:
    """Return an upsert SQL statement for the current database dialect.

    Works for SQLite, PostgreSQL (both support ``ON CONFLICT``), and MySQL
    (uses ``ON DUPLICATE KEY UPDATE``).
    """
    placeholders = ", ".join("?" * len(columns))
    col_list = ", ".join(columns)
    dialect = get_dialect_name()

    if dialect in ("mysql", "mariadb"):
        # MySQL 8.0.20+ deprecates VALUES(col); use an aliased row reference instead.
        update_set = ", ".join(f"{c}=new_row.{c}" for c in columns if c != id_col)
        return (
            f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) AS new_row "
            f"ON DUPLICATE KEY UPDATE {update_set}"
        )
    # SQLite and PostgreSQL both support ON CONFLICT
    update_set = ", ".join(f"{c}=excluded.{c}" for c in columns if c != id_col)
    return (
        f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
        f"ON CONFLICT({id_col}) DO UPDATE SET {update_set}"
    )


def build_insert_ignore_sql(table: str, columns: list) -> str:
    """Return an insert-or-ignore SQL statement for the current database dialect."""
    placeholders = ", ".join("?" * len(columns))
    col_list = ", ".join(columns)
    dialect = get_dialect_name()

    if dialect == "postgresql":
        return f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) ON CONFLICT DO NOTHING"
    if dialect in ("mysql", "mariadb"):
        return f"INSERT IGNORE INTO {table} ({col_list}) VALUES ({placeholders})"
    # SQLite default
    return f"INSERT OR IGNORE INTO {table} ({col_list}) VALUES ({placeholders})"


# ---------------------------------------------------------------------------
# Async engine + session (used by the Discord bot in bots/flight_logger.py)
# ---------------------------------------------------------------------------

_async_engine = None


def get_async_engine():
    """Return the singleton async SQLAlchemy engine for use with asyncio code."""
    global _async_engine
    if _async_engine is None:
        from sqlalchemy.ext.asyncio import create_async_engine as _cae
        url = get_async_db_url()
        kwargs: dict = {}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
        _async_engine = _cae(url, **kwargs)
        safe_url = re.sub(r":[^@/]+@", ":***@", url)
        logger.info("Async database engine initialised: %s", safe_url)
    return _async_engine
