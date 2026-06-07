"""SQLite to MariaDB migration helpers."""

import datetime as dt
import logging
import os
import re
import shutil
import sqlite3
from typing import Any

logger = logging.getLogger("DBMigration")

VOLATILE_TABLES = {"command_claims"}
MAX_INDEXED_VARCHAR_LENGTH = 191


def _quote_identifier(name: str) -> str:
    return f"`{str(name).replace('`', '``')}`"


def _quote_sqlite_identifier(name: str) -> str:
    return f'"{str(name).replace(chr(34), chr(34) + chr(34))}"'


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


def _varchar_length(mysql_type: str) -> int | None:
    match = re.fullmatch(r"VARCHAR\((\d+)\)", mysql_type.strip(), flags=re.I)
    return int(match.group(1)) if match else None


def _cap_indexed_varchar(mysql_type: str, *, indexed: bool) -> str:
    length = _varchar_length(mysql_type)
    if indexed and length and length > MAX_INDEXED_VARCHAR_LENGTH:
        return f"VARCHAR({MAX_INDEXED_VARCHAR_LENGTH})"
    return mysql_type


def _model_column_mysql_types() -> dict[tuple[str, str], str]:
    """Return preferred MariaDB types from SQLAlchemy models when available."""
    try:
        from sqlalchemy import BigInteger, Float, Integer, String, Text
        from utils.db_models import Base
    except Exception:
        return {}

    result: dict[tuple[str, str], str] = {}
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            col_type = column.type
            mysql_type = None
            if isinstance(col_type, BigInteger):
                mysql_type = "BIGINT"
            elif isinstance(col_type, Integer):
                mysql_type = "INTEGER"
            elif isinstance(col_type, Float):
                mysql_type = "DOUBLE"
            elif isinstance(col_type, Text):
                mysql_type = "LONGTEXT"
            elif isinstance(col_type, String):
                mysql_type = f"VARCHAR({col_type.length or 255})"
            if mysql_type:
                result[(table.name, column.name)] = mysql_type
    return result


def _model_indexed_columns() -> dict[str, set[str]]:
    try:
        from utils.db_models import Base
    except Exception:
        return {}

    by_table: dict[str, set[str]] = {}
    for table in Base.metadata.sorted_tables:
        indexed = by_table.setdefault(table.name, set())
        for column in table.columns:
            if column.primary_key or column.index or column.unique:
                indexed.add(column.name)
        for index in table.indexes:
            indexed.update(column.name for column in index.columns)
    return by_table


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
    model_types = _model_column_mysql_types()
    indexed_columns = _model_indexed_columns().get(table_name, set()) | {c["name"] for c in pk_columns}

    lines: list[str] = []
    for col in columns:
        name = col["name"]
        sqlite_type = col["type"] or ""
        is_single_pk_col = single_pk and name == single_pk_name
        mysql_type = model_types.get(
            (table_name, name),
            _map_sqlite_type(sqlite_type, is_primary_key=bool(col["pk"])),
        )
        mysql_type = _cap_indexed_varchar(mysql_type, indexed=name in indexed_columns)
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


def inspect_sqlite_source(sqlite_path: str) -> dict[str, Any]:
    """Return table, column, index, and row-count information for a SQLite file."""
    if not os.path.exists(sqlite_path):
        raise FileNotFoundError(f"SQLite database not found: {sqlite_path}")

    conn = sqlite3.connect(sqlite_path)
    conn.row_factory = sqlite3.Row
    try:
        table_meta = conn.execute(
            """
            SELECT name, sql
            FROM sqlite_master
            WHERE type='table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        ).fetchall()

        tables: dict[str, Any] = {}
        for row in table_meta:
            table_name = row["name"]
            columns = [
                {
                    "name": c[1],
                    "type": c[2],
                    "notnull": c[3],
                    "dflt_value": c[4],
                    "pk": c[5],
                }
                for c in conn.execute(f"PRAGMA table_info({_quote_sqlite_identifier(table_name)})").fetchall()
            ]
            count = conn.execute(f"SELECT COUNT(*) FROM {_quote_sqlite_identifier(table_name)}").fetchone()[0]
            tables[table_name] = {
                "rows": int(count or 0),
                "columns": columns,
                "sql": row["sql"] or "",
                "skipped": table_name in VOLATILE_TABLES,
                "indexes": _sqlite_index_specs(conn, table_name),
            }

        return {
            "sqlite_path": sqlite_path,
            "sqlite_exists": True,
            "tables": tables,
            "total_rows": sum(t["rows"] for t in tables.values()),
            "persistent_rows": sum(t["rows"] for t in tables.values() if not t["skipped"]),
        }
    finally:
        conn.close()


def backup_sqlite_database(sqlite_path: str, backup_dir: str | None = None) -> str:
    """Copy the SQLite file into a timestamped backup and return the backup path."""
    if not os.path.exists(sqlite_path):
        raise FileNotFoundError(f"SQLite database not found: {sqlite_path}")

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(sqlite_path)))
    backup_dir = backup_dir or os.path.join(project_root, "backups")
    os.makedirs(backup_dir, exist_ok=True)
    stem, ext = os.path.splitext(os.path.basename(sqlite_path))
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup_path = os.path.join(backup_dir, f"{stem}-{timestamp}{ext or '.db'}")
    shutil.copy2(sqlite_path, backup_path)
    return backup_path


def _connect_mariadb(host: str, port: int, user: str, password: str, database: str | None = None):
    import pymysql  # lazy import for optional dependency

    kwargs = {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "autocommit": database is None,
        "charset": "utf8mb4",
    }
    if database:
        kwargs["database"] = database
        kwargs["autocommit"] = False
    return pymysql.connect(**kwargs)


def _sqlite_index_specs(sqlite_conn: sqlite3.Connection, table_name: str) -> list[dict[str, Any]]:
    indexes: list[dict[str, Any]] = []
    for idx in sqlite_conn.execute(f"PRAGMA index_list({_quote_sqlite_identifier(table_name)})").fetchall():
        idx_name = idx[1]
        if str(idx_name).startswith("sqlite_autoindex_"):
            continue
        columns = [
            col[2]
            for col in sqlite_conn.execute(f"PRAGMA index_info({_quote_sqlite_identifier(idx_name)})").fetchall()
            if col[2]
        ]
        if not columns:
            continue
        indexes.append({
            "name": idx_name,
            "unique": bool(idx[2]),
            "columns": columns,
        })
    return indexes


def _model_index_specs() -> dict[str, list[dict[str, Any]]]:
    """Return SQLAlchemy model indexes so MariaDB gets current app indexes too."""
    try:
        from utils.db_models import Base
    except Exception:
        return {}

    by_table: dict[str, list[dict[str, Any]]] = {}
    for table in Base.metadata.sorted_tables:
        specs = by_table.setdefault(table.name, [])
        for index in table.indexes:
            specs.append({
                "name": index.name or f"ix_{table.name}_{'_'.join(c.name for c in index.columns)}",
                "unique": bool(index.unique),
                "columns": [c.name for c in index.columns],
            })
        for column in table.columns:
            if column.index:
                specs.append({
                    "name": f"ix_{table.name}_{column.name}",
                    "unique": bool(column.unique),
                    "columns": [column.name],
                })
    return by_table


def _create_indexes(cur, table_name: str, index_specs: list[dict[str, Any]]) -> int:
    created = 0
    seen = set()
    for spec in index_specs:
        columns = [c for c in spec.get("columns", []) if c]
        if not columns:
            continue
        key = (spec.get("name"), tuple(columns))
        if key in seen:
            continue
        seen.add(key)
        index_name_raw = spec.get("name") or f"ix_{table_name}_{'_'.join(columns)}"
        cur.execute(f"SHOW INDEX FROM {_quote_identifier(table_name)} WHERE Key_name = %s", (index_name_raw,))
        if cur.fetchone():
            continue
        unique = "UNIQUE " if spec.get("unique") else ""
        index_name = _quote_identifier(index_name_raw)
        cols_sql = ", ".join(_quote_identifier(c) for c in columns)
        cur.execute(
            f"CREATE {unique}INDEX {index_name} "
            f"ON {_quote_identifier(table_name)} ({cols_sql})"
        )
        created += 1
    return created


def _ensure_indexable_column_types(cur, table_name: str, columns: list[str]) -> None:
    if not columns:
        return
    cur.execute(f"SHOW COLUMNS FROM {_quote_identifier(table_name)}")
    column_meta = {row[0]: row for row in cur.fetchall()}
    for column in dict.fromkeys(columns):
        meta = column_meta.get(column)
        if not meta:
            continue
        column_type = str(meta[1] or "").lower()
        if column_type in {"text", "mediumtext", "longtext"}:
            null_sql = "NULL" if str(meta[2]).upper() == "YES" else "NOT NULL"
            default_sql = ""
            if meta[4] is not None:
                default = str(meta[4]).replace("'", "''")
                default_sql = f" DEFAULT '{default}'"
            cur.execute(
                f"ALTER TABLE {_quote_identifier(table_name)} "
                f"MODIFY {_quote_identifier(column)} VARCHAR({MAX_INDEXED_VARCHAR_LENGTH}) {null_sql}{default_sql}"
            )
            continue
        length = _varchar_length(column_type)
        if length and length > MAX_INDEXED_VARCHAR_LENGTH:
            null_sql = "NULL" if str(meta[2]).upper() == "YES" else "NOT NULL"
            default_sql = ""
            if meta[4] is not None:
                default = str(meta[4]).replace("'", "''")
                default_sql = f" DEFAULT '{default}'"
            cur.execute(
                f"ALTER TABLE {_quote_identifier(table_name)} "
                f"MODIFY {_quote_identifier(column)} VARCHAR({MAX_INDEXED_VARCHAR_LENGTH}) {null_sql}{default_sql}"
            )


def _target_table_columns(cur, database: str, table_name: str) -> dict[str, str]:
    cur.execute(
        """
        SELECT COLUMN_NAME, COLUMN_TYPE
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
        ORDER BY ORDINAL_POSITION
        """,
        (database, table_name),
    )
    return {name: column_type for name, column_type in cur.fetchall()}


def _target_table_counts(cur, database: str, table_names: list[str]) -> dict[str, int | None]:
    counts: dict[str, int | None] = {}
    for table_name in table_names:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
            """,
            (database, table_name),
        )
        if not cur.fetchone()[0]:
            counts[table_name] = None
            continue
        cur.execute(f"SELECT COUNT(*) FROM {_quote_identifier(table_name)}")
        counts[table_name] = int(cur.fetchone()[0] or 0)
    return counts


def dry_run_sqlite_to_mariadb(
    sqlite_path: str,
    host: str,
    port: int,
    user: str,
    password: str,
    database: str,
) -> dict[str, Any]:
    """Inspect source and target without changing MariaDB."""
    if not host or not user or not database:
        raise ValueError("Missing MariaDB connection settings (host/user/database).")

    source = inspect_sqlite_source(sqlite_path)
    target_tables: dict[str, int | None] = {}
    schema_drift: dict[str, Any] = {}

    root_conn = _connect_mariadb(host, port, user, password)
    try:
        with root_conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                FROM information_schema.SCHEMATA
                WHERE SCHEMA_NAME = %s
                """,
                (database,),
            )
            target_database_exists = bool(cur.fetchone()[0])
    finally:
        root_conn.close()

    if target_database_exists:
        maria_conn = _connect_mariadb(host, port, user, password, database)
        try:
            with maria_conn.cursor() as cur:
                persistent_tables = [name for name, meta in source["tables"].items() if not meta["skipped"]]
                target_tables = _target_table_counts(cur, database, persistent_tables)
                for table_name in persistent_tables:
                    target_cols = _target_table_columns(cur, database, table_name)
                    if not target_cols:
                        continue
                    model_types = _model_column_mysql_types()
                    expected_cols = {
                        col["name"]: model_types.get(
                            (table_name, col["name"]),
                            _map_sqlite_type(col["type"] or "", is_primary_key=bool(col["pk"])),
                        )
                        for col in source["tables"][table_name]["columns"]
                    }
                    missing = sorted(set(expected_cols) - set(target_cols))
                    extra = sorted(set(target_cols) - set(expected_cols))
                    changed = {
                        name: {"expected": expected_cols[name], "actual": target_cols[name]}
                        for name in sorted(set(expected_cols) & set(target_cols))
                        if expected_cols[name].lower().split("(")[0] not in target_cols[name].lower()
                    }
                    if missing or extra or changed:
                        schema_drift[table_name] = {
                            "missing_columns": missing,
                            "extra_columns": extra,
                            "changed_types": changed,
                        }
        finally:
            maria_conn.close()

    return {
        "ok": True,
        "dry_run": True,
        "source": source,
        "target_database_exists": target_database_exists,
        "target_tables": target_tables,
        "schema_drift": schema_drift,
        "warnings": [
            "Target database does not exist yet; migration will create it."
        ] if not target_database_exists else [],
    }


def validate_mariadb_counts(
    sqlite_path: str,
    host: str,
    port: int,
    user: str,
    password: str,
    database: str,
) -> dict[str, Any]:
    """Compare persistent SQLite row counts with MariaDB target row counts."""
    source = inspect_sqlite_source(sqlite_path)
    persistent_tables = [name for name, meta in source["tables"].items() if not meta["skipped"]]
    maria_conn = _connect_mariadb(host, port, user, password, database)
    try:
        with maria_conn.cursor() as cur:
            target_counts = _target_table_counts(cur, database, persistent_tables)
    finally:
        maria_conn.close()

    tables: dict[str, Any] = {}
    mismatches = []
    for table_name in persistent_tables:
        source_count = int(source["tables"][table_name]["rows"])
        target_count = target_counts.get(table_name)
        ok = target_count == source_count
        tables[table_name] = {
            "source_rows": source_count,
            "target_rows": target_count,
            "ok": ok,
        }
        if not ok:
            mismatches.append(table_name)

    return {
        "ok": not mismatches,
        "tables": tables,
        "mismatches": mismatches,
        "source_total_rows": sum(v["source_rows"] for v in tables.values()),
        "target_total_rows": sum(v["target_rows"] or 0 for v in tables.values()),
    }


def migrate_sqlite_to_mariadb_detailed(
    sqlite_path: str,
    host: str,
    port: int,
    user: str,
    password: str,
    database: str,
    truncate_before_import: bool = True,
    create_backup: bool = True,
    create_indexes: bool = True,
    validate_after_import: bool = True,
) -> dict[str, Any]:
    """Run a full migration and return backup, index, and validation details."""
    started_at = dt.datetime.now(dt.timezone.utc).isoformat()
    source = inspect_sqlite_source(sqlite_path)
    backup_path = backup_sqlite_database(sqlite_path) if create_backup else None
    tables = migrate_sqlite_to_mariadb(
        sqlite_path=sqlite_path,
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
        truncate_before_import=truncate_before_import,
        create_indexes=create_indexes,
    )
    validation = (
        validate_mariadb_counts(sqlite_path, host, port, user, password, database)
        if validate_after_import
        else None
    )
    return {
        "ok": bool(validation["ok"]) if validation else True,
        "dry_run": False,
        "started_at": started_at,
        "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "sqlite_path": sqlite_path,
        "backup_path": backup_path,
        "truncate_before_import": truncate_before_import,
        "tables": tables,
        "total_rows_copied": sum(tables.values()),
        "source_total_rows": source["persistent_rows"],
        "validation": validation,
    }


def migrate_sqlite_to_mariadb(
    sqlite_path: str,
    host: str,
    port: int,
    user: str,
    password: str,
    database: str,
    truncate_before_import: bool = True,
    create_indexes: bool = True,
) -> dict[str, int]:
    """Migrate persistent user tables from sqlite to MariaDB.

    Returns a mapping of table name to rows inserted.
    """
    if not os.path.exists(sqlite_path):
        raise FileNotFoundError(f"SQLite database not found: {sqlite_path}")
    if not host or not user or not database:
        raise ValueError("Missing MariaDB connection settings (host/user/database).")

    logger.info("[MIGRATE] Opening SQLite DB: %s", sqlite_path)
    sqlite_conn = sqlite3.connect(sqlite_path)
    sqlite_conn.row_factory = sqlite3.Row

    root_conn = None
    maria_conn = None

    try:
        root_conn = _connect_mariadb(host, port, user, password)
        with root_conn.cursor() as cur:
            cur.execute(f"CREATE DATABASE IF NOT EXISTS {_quote_identifier(database)} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")

        maria_conn = _connect_mariadb(host, port, user, password, database)

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

                pragma = sqlite_conn.execute(f"PRAGMA table_info({_quote_sqlite_identifier(table_name)})").fetchall()
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

                if create_indexes:
                    sqlite_indexes = _sqlite_index_specs(sqlite_conn, table_name)
                    model_indexes = _model_index_specs().get(table_name, [])
                    index_columns = [
                        column
                        for spec in sqlite_indexes + model_indexes
                        for column in spec.get("columns", [])
                    ] + [c["name"] for c in columns if c["pk"]]
                    _ensure_indexable_column_types(cur, table_name, index_columns)
                    created_count = _create_indexes(cur, table_name, sqlite_indexes + model_indexes)
                    if created_count:
                        logger.info("[MIGRATE] %s: ensured %d index(es).", table_name, created_count)

                if truncate_before_import:
                    cur.execute(f"TRUNCATE TABLE {_quote_identifier(table_name)}")

                src_rows = sqlite_conn.execute(f"SELECT * FROM {_quote_sqlite_identifier(table_name)}").fetchall()
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
