"""SQLite to MariaDB migration helpers."""

import logging
import os
import re
import sqlite3
from typing import Any

logger = logging.getLogger("DBMigration")

VOLATILE_TABLES = {"command_claims"}


def _quote_identifier(name: str) -> str:
    return f"`{str(name).replace('`', '``')}`"


def _map_sqlite_type(sqlite_decl: str, *, is_primary_key: bool = False) -> str:
    t = (sqlite_decl or "").strip().upper()
    if is_primary_key and any(x in t for x in ("CHAR", "CLOB", "TEXT")):
        return "VARCHAR(255)"
    if "INT" in t:
        return "BIGINT"
    if any(x in t for x in ("CHAR", "CLOB", "TEXT")):
        return "LONGTEXT"
    if "BLOB" in t or not t:
        return "LONGBLOB" if "BLOB" in t else "LONGTEXT"
    if any(x in t for x in ("REAL", "FLOA", "DOUB")):
        return "DOUBLE"
    if any(x in t for x in ("NUMERIC", "DECIMAL")):
        return "DECIMAL(38, 10)"
    if "BOOL" in t:
        return "TINYINT(1)"
    if any(x in t for x in ("DATE", "TIME")):
        return "DATETIME"
    return "LONGTEXT"


def _supports_default(mysql_type: str) -> bool:
    """MariaDB cannot use defaults for BLOB/TEXT columns on many deployments."""
    normalized = mysql_type.upper()
    return not any(x in normalized for x in ("TEXT", "BLOB"))


def _translate_default(default_value: Any) -> str:
    if default_value is None:
        return ""

    raw = str(default_value).strip()
    upper = raw.upper()

    if upper in {"NULL", "CURRENT_TIMESTAMP", "CURRENT_TIMESTAMP()"}:
        return f" DEFAULT {upper}"

    # Numeric literal
    if re.fullmatch(r"[-+]?\d+(\.\d+)?", raw):
        return f" DEFAULT {raw}"

    # Already quoted literal from sqlite schema
    if (raw.startswith("'") and raw.endswith("'")) or (raw.startswith('"') and raw.endswith('"')):
        return f" DEFAULT {raw}"

    escaped = raw.replace("'", "''")
    return f" DEFAULT '{escaped}'"


def _build_create_table_sql(table_name: str, columns: list[dict], sqlite_table_sql: str) -> str:
    table_sql_upper = (sqlite_table_sql or "").upper()
    has_autoincrement = "AUTOINCREMENT" in table_sql_upper

    pk_columns = sorted([c for c in columns if c["pk"] > 0], key=lambda c: c["pk"])
    single_pk = len(pk_columns) == 1
    single_pk_name = pk_columns[0]["name"] if single_pk else None

    lines: list[str] = []
    for col in columns:
        name = col["name"]
        sqlite_type = col["type"] or ""
        is_single_pk_col = single_pk and name == single_pk_name
        mysql_type = _map_sqlite_type(sqlite_type, is_primary_key=bool(col["pk"]))
        not_null = " NOT NULL" if col["notnull"] else ""

        is_int_pk = "INT" in sqlite_type.upper()

        pk_suffix = ""
        auto_suffix = ""
        default_clause = _translate_default(col["dflt_value"])
        if default_clause and not _supports_default(mysql_type):
            default_clause = ""

        if is_single_pk_col:
            pk_suffix = " PRIMARY KEY"
            # MariaDB requires integer primary key for AUTO_INCREMENT.
            if is_int_pk or mysql_type.startswith("BIGINT"):
                auto_suffix = " AUTO_INCREMENT" if has_autoincrement else ""
            # Avoid invalid defaults on primary key columns.
            default_clause = ""

        lines.append(
            f"  {_quote_identifier(name)} {mysql_type}{not_null}{default_clause}{pk_suffix}{auto_suffix}"
        )

    if not single_pk and pk_columns:
        pk_cols_sql = ", ".join(_quote_identifier(c["name"]) for c in pk_columns)
        lines.append(f"  PRIMARY KEY ({pk_cols_sql})")

    if table_name == "warnings" and not any(c["name"] == "id" for c in columns):
        lines.insert(0, "  `id` BIGINT PRIMARY KEY AUTO_INCREMENT")

    cols_sql = ",\n".join(lines)
    return (
        f"CREATE TABLE IF NOT EXISTS {_quote_identifier(table_name)} (\n"
        f"{cols_sql}\n"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
    )


def migrate_sqlite_to_mariadb(
    sqlite_path: str,
    host: str,
    port: int,
    user: str,
    password: str,
    database: str,
    truncate_before_import: bool = True,
) -> dict[str, int]:
    """Migrate persistent user tables from sqlite to MariaDB.

    Returns a mapping of table name to rows inserted.
    """
    if not os.path.exists(sqlite_path):
        raise FileNotFoundError(f"SQLite database not found: {sqlite_path}")
    if not host or not user or not database:
        raise ValueError("Missing MariaDB connection settings (host/user/database).")

    import pymysql  # lazy import for optional dependency

    logger.info("[MIGRATE] Opening SQLite DB: %s", sqlite_path)
    sqlite_conn = sqlite3.connect(sqlite_path)
    sqlite_conn.row_factory = sqlite3.Row

    root_conn = None
    maria_conn = None

    try:
        root_conn = pymysql.connect(
            host=host,
            port=port,
            user=user,
            password=password,
            autocommit=True,
            charset="utf8mb4",
        )
        with root_conn.cursor() as cur:
            cur.execute(f"CREATE DATABASE IF NOT EXISTS {_quote_identifier(database)} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")

        maria_conn = pymysql.connect(
            host=host,
            port=port,
            user=user,
            password=password,
            database=database,
            autocommit=False,
            charset="utf8mb4",
        )

        table_rows: dict[str, int] = {}

        with sqlite_conn:
            table_meta = sqlite_conn.execute(
                """
                SELECT name, sql
                FROM sqlite_master
                WHERE type='table' AND name NOT LIKE 'sqlite_%'
                ORDER BY name
                """
            ).fetchall()

        if not table_meta:
            logger.warning("[MIGRATE] No user tables found in SQLite database.")
            return {}

        with maria_conn.cursor() as cur:
            cur.execute("SET FOREIGN_KEY_CHECKS=0")

            for row in table_meta:
                table_name = row["name"]
                if table_name in VOLATILE_TABLES:
                    table_rows[table_name] = 0
                    logger.info("[MIGRATE] Skipping volatile table %s.", table_name)
                    continue

                sqlite_table_sql = row["sql"] or ""

                pragma = sqlite_conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()
                columns = [
                    {
                        "name": c[1],
                        "type": c[2],
                        "notnull": c[3],
                        "dflt_value": c[4],
                        "pk": c[5],
                    }
                    for c in pragma
                ]
                if not columns:
                    logger.info("[MIGRATE] Skipping table %s (no columns).", table_name)
                    continue

                create_sql = _build_create_table_sql(table_name, columns, sqlite_table_sql)
                cur.execute(create_sql)

                if truncate_before_import:
                    cur.execute(f"TRUNCATE TABLE {_quote_identifier(table_name)}")

                src_rows = sqlite_conn.execute(f'SELECT * FROM "{table_name}"').fetchall()
                if not src_rows:
                    table_rows[table_name] = 0
                    logger.info("[MIGRATE] %s: 0 rows.", table_name)
                    continue

                col_names = [c["name"] for c in columns]
                col_sql = ", ".join(_quote_identifier(name) for name in col_names)
                placeholders = ", ".join(["%s"] * len(col_names))
                insert_verb = "INSERT" if truncate_before_import else "INSERT IGNORE"
                insert_sql = f"{insert_verb} INTO {_quote_identifier(table_name)} ({col_sql}) VALUES ({placeholders})"

                payload = [tuple(r[name] for name in col_names) for r in src_rows]
                cur.executemany(insert_sql, payload)
                copied_rows = len(payload) if truncate_before_import else max(cur.rowcount, 0)
                table_rows[table_name] = copied_rows
                if copied_rows != len(payload):
                    logger.info(
                        "[MIGRATE] %s: %d/%d rows inserted (%d duplicate row(s) ignored).",
                        table_name,
                        copied_rows,
                        len(payload),
                        len(payload) - copied_rows,
                    )
                else:
                    logger.info("[MIGRATE] %s: %d rows.", table_name, copied_rows)

            cur.execute("SET FOREIGN_KEY_CHECKS=1")

        maria_conn.commit()
        logger.info("[MIGRATE] Migration complete. %d table(s) migrated.", len(table_rows))
        return table_rows

    except Exception:
        if maria_conn:
            maria_conn.rollback()
        raise
    finally:
        if sqlite_conn:
            sqlite_conn.close()
        if maria_conn:
            maria_conn.close()
        if root_conn:
            root_conn.close()
