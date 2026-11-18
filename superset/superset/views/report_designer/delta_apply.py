# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
"""Apply an incremental (delta) CSV to a table the sync already created.

The Moodle "Sync DB" pass used to re-send every table in full on every run. It
now sends only the rows that changed since the last successful pass, and this
module writes them into the destination table reports already point at:

``upsert``
    The CSV holds changed and new rows. They are staged into a temporary table,
    aligned to the destination's column types (exactly like an appended upload
    part) and merged with ``INSERT … SELECT … ON DUPLICATE KEY UPDATE`` (MySQL
    /MariaDB) or ``ON CONFLICT (key) DO UPDATE`` (PostgreSQL), a bounded number
    of rows per statement so the merge cannot hold a long lock.

``prune``
    The CSV holds every key the source still has. Anything on the destination
    that is not in it was deleted in Moodle and is removed.

``replace``
    Delegates to :func:`ingest_excel`, i.e. the historic whole-file behaviour.

An upsert without a unique key on the matched column would silently duplicate
rows, so the key is verified first and a clear error is raised when it is
missing.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any

from sqlalchemy import inspect, text

from superset.models.core import Database

from .excel_ingest import (
    _align_to_existing,
    _read_dataframe,
    IngestError,
    ingest_excel,
)

logger = logging.getLogger(__name__)

#: Rows per INSERT…SELECT statement. Bounds how long one statement can lock.
CHUNK_ROWS = 5000

#: Accepted modes.
MODES = ("upsert", "prune", "replace")

#: MySQL/MariaDB identifiers are limited to 64 characters.
MAX_IDENTIFIER = 64


class DeltaError(Exception):
    """Raised when a delta cannot be applied."""


def _quote(identifier: str, dialect: str) -> str:
    """Quote one identifier for the target dialect."""
    clean = re.sub(r"[^A-Za-z0-9_]", "", identifier or "")
    if not clean:
        raise DeltaError(f"invalid identifier: {identifier!r}")
    if dialect in ("mysql", "mariadb"):
        return f"`{clean}`"
    return f'"{clean}"'


def _qualified(table: str, schema: str | None, dialect: str) -> str:
    """``schema.table`` quoted for the target dialect."""
    quoted = _quote(table, dialect)
    if schema:
        return f"{_quote(schema, dialect)}.{quoted}"
    return quoted


def _clean_table(name: str) -> str:
    """Normalise a table name the same way excel_ingest does."""
    clean = re.sub(r"[^a-z0-9_]", "_", (name or "").lower())
    if not clean:
        raise DeltaError("table name is required")
    if not clean[0].isalpha():
        clean = f"xp_{clean}"
    return clean


def _temp_name(table: str) -> str:
    """Name of the staging table for this delta."""
    suffix = f"__delta_{os.getpid()}_{int(time.time()) % 100000}"
    room = MAX_IDENTIFIER - len(suffix)
    return f"{table[:room]}{suffix}"


def _has_unique_key(inspector, table: str, schema: str | None, key: str) -> bool:
    """Is there a single-column unique key on ``key``?

    Without one ``ON DUPLICATE KEY UPDATE`` never matches, so every re-sent row
    would be inserted a second time and the report would double-count.
    """
    wanted = [key.lower()]
    primary = (inspector.get_pk_constraint(table, schema=schema) or {}).get(
        "constrained_columns"
    ) or []
    if [str(column).lower() for column in primary] == wanted:
        return True
    for index in inspector.get_indexes(table, schema=schema) or []:
        if not index.get("unique"):
            continue
        columns = [str(column).lower() for column in index.get("column_names") or []]
        if columns == wanted:
            return True
    return False


def _upsert_sql(qualified: str, quoted_temp: str, columns: list[str], key: str,
                dialect: str, limit: int, offset: int) -> str:
    """One bounded merge statement."""
    dialect = (dialect or "").lower()
    collist = ", ".join(_quote(column, dialect) for column in columns)
    updates = [
        column for column in columns if column.lower() != key.lower()
    ]
    # A stable order is what makes LIMIT/OFFSET chunking cover every row once.
    select = (
        f"SELECT {collist} FROM {quoted_temp} "
        f"ORDER BY {_quote(key, dialect)} LIMIT {limit} OFFSET {offset}"
    )
    if dialect in ("mysql", "mariadb"):
        if updates:
            assignment = ", ".join(
                f"{_quote(column, dialect)} = VALUES({_quote(column, dialect)})"
                for column in updates
            )
        else:
            # Nothing but the key in the file: re-affirming the row is a no-op,
            # but the statement still has to be valid.
            assignment = f"{_quote(key, dialect)} = {_quote(key, dialect)}"
        return (
            f"INSERT INTO {qualified} ({collist}) {select} "
            f"ON DUPLICATE KEY UPDATE {assignment}"
        )
    if updates:
        assignment = ", ".join(
            f"{_quote(column, dialect)} = EXCLUDED.{_quote(column, dialect)}"
            for column in updates
        )
    else:
        assignment = f"{_quote(key, dialect)} = EXCLUDED.{_quote(key, dialect)}"
    return (
        f"INSERT INTO {qualified} ({collist}) {select} "
        f"ON CONFLICT ({_quote(key, dialect)}) DO UPDATE SET {assignment}"
    )


def _prune_sql(qualified: str, quoted_temp: str, key: str, dialect: str) -> str:
    """Delete every destination row whose key the source no longer has."""
    dialect = (dialect or "").lower()
    qkey = _quote(key, dialect)
    if dialect in ("mysql", "mariadb"):
        # MySQL cannot DELETE from the table it joins to, but a left join onto
        # the staging table is exactly "keys that are not in the source".
        return (
            f"DELETE t FROM {qualified} t LEFT JOIN {quoted_temp} s "
            f"ON s.{qkey} = t.{qkey} WHERE s.{qkey} IS NULL"
        )
    return (
        f"DELETE FROM {qualified} t WHERE NOT EXISTS ("
        f"SELECT 1 FROM {quoted_temp} s WHERE s.{qkey} = t.{qkey})"
    )


def apply_delta(
    database: Database,
    table: str,
    schema: str | None,
    file_stream: Any,
    key: str,
    mode: str = "upsert",
) -> dict[str, Any]:
    """Merge one delta CSV into ``schema.table``.

    ``file_stream`` is the uploaded part (a plain CSV built by the Moodle
    exporter, so the same values and the same blank handling as an upload part).

    Returns ``{"table", "mode", "rows", "inserted", "updated", "deleted", "ms"}``.
    """
    started = time.time()
    mode = (mode or "upsert").lower()
    if mode not in MODES:
        raise DeltaError(f"unknown mode: {mode!r}")
    clean = _clean_table(table)
    key = re.sub(r"[^A-Za-z0-9_]", "", key or "")
    if not key and mode != "replace":
        raise DeltaError("key column is required")

    if mode == "replace":
        # The whole-file path: unchanged behaviour, kept so a delta caller can
        # always fall back to a full ingest.
        result = ingest_excel(
            database,
            file_stream,
            f"{clean}.csv",
            table_name=clean,
            schema=schema,
            mode="replace",
        )
        return {
            "table": clean,
            "mode": "replace",
            "rows": int(result.get("rows") or 0),
            "inserted": int(result.get("rows") or 0),
            "updated": 0,
            "deleted": 0,
            "ms": int((time.time() - started) * 1000),
        }

    try:
        df = _read_dataframe(file_stream, f"{clean}.csv")
    except IngestError:
        raise
    if df is None or df.empty:
        raise DeltaError("the delta file has no rows")

    with database.get_sqla_engine(schema=schema) as engine:
        dialect = (engine.dialect.name or "").lower()
        inspector = inspect(engine)
        if not inspector.has_table(clean, schema=schema):
            raise DeltaError(
                f"{clean} does not exist on the target yet; run a full sync first"
            )
        target_columns = [
            str(column["name"])
            for column in inspector.get_columns(clean, schema=schema)
        ]
        lowered = {column.lower(): column for column in target_columns}

        # Only the columns both sides know: an extra CSV column would make the
        # INSERT fail, and a column the file does not carry must be left alone
        # rather than blanked.
        wanted: list[str] = []
        for column in df.columns:
            match = lowered.get(str(column).lower())
            if match is not None:
                wanted.append(match)
        if key.lower() not in lowered:
            raise DeltaError(f"{clean} has no column {key!r}")
        key = lowered[key.lower()]
        if key not in wanted:
            raise DeltaError(f"the delta file does not carry the key column {key!r}")
        df = df[wanted]

        if mode == "upsert" and not _has_unique_key(inspector, clean, schema, key):
            raise DeltaError(
                f"{clean} has no unique key on {key!r}, so an incremental update "
                "would duplicate rows; run a full sync for this table instead"
            )

        qualified = _qualified(clean, schema, dialect)
        temp = _temp_name(clean)
        quoted_temp = _qualified(temp, schema, dialect)

        try:
            # Same type alignment an appended upload part gets, so dates,
            # numbers and the NULL/'' distinction match the destination.
            df = _align_to_existing(df, engine, clean, schema)
            df.to_sql(
                temp,
                con=engine,
                schema=schema,
                if_exists="replace",
                index=False,
                chunksize=500,
            )
            rows = int(len(df))

            if mode == "prune":
                with engine.begin() as connection:
                    result = connection.execute(text(_prune_sql(
                        qualified, quoted_temp, key, dialect)))
                deleted = max(0, int(result.rowcount or 0))
                logger.info("Pruned %s: %d row(s) removed", clean, deleted)
                return {
                    "table": clean,
                    "mode": mode,
                    "rows": rows,
                    "inserted": 0,
                    "updated": 0,
                    "deleted": deleted,
                    "ms": int((time.time() - started) * 1000),
                }

            # How many of these keys the destination already has decides the
            # split between "new" and "changed": rowcount alone cannot say (a
            # MySQL upsert counts 1 per insert and 2 per update).
            with engine.connect() as connection:
                already = connection.execute(text(
                    f"SELECT COUNT(*) FROM {qualified} t "
                    f"INNER JOIN {quoted_temp} s ON s.{_quote(key, dialect)} = "
                    f"t.{_quote(key, dialect)}"
                )).scalar()
            already = int(already or 0)
            inserted = max(0, rows - already)
            updated = min(rows, already)

            offset = 0
            while offset < rows:
                with engine.begin() as connection:
                    connection.execute(text(_upsert_sql(
                        qualified, quoted_temp, wanted, key, dialect, CHUNK_ROWS, offset
                    )))
                offset += CHUNK_ROWS
            logger.info(
                "Merged %s: %d new, %d changed (%d row(s) sent)",
                clean,
                inserted,
                updated,
                rows,
            )
            return {
                "table": clean,
                "mode": mode,
                "rows": rows,
                "inserted": inserted,
                "updated": updated,
                "deleted": 0,
                "ms": int((time.time() - started) * 1000),
            }
        finally:
            # The staging table must never survive a failure: it is a real table
            # in the reporting schema, and its name is reused by the next part.
            try:
                with engine.begin() as connection:
                    connection.execute(text(f"DROP TABLE IF EXISTS {quoted_temp}"))
            except Exception:  # pylint: disable=broad-except
                logger.warning("Could not drop staging table %s", quoted_temp)


def table_stats(
    database: Database, schema: str | None, tables: list[str]
) -> dict[str, dict[str, Any]]:
    """Row count and highest key of each destination table.

    Answers the question the prune pass asks — "does the copy still hold the
    same number of rows as the source?" — without transferring a single row.
    A table that does not exist yet is simply absent from the result.
    """
    stats: dict[str, dict[str, Any]] = {}
    with database.get_sqla_engine(schema=schema) as engine:
        dialect = (engine.dialect.name or "").lower()
        inspector = inspect(engine)
        for raw in tables or []:
            try:
                table = _clean_table(str(raw))
            except DeltaError:
                continue
            if not inspector.has_table(table, schema=schema):
                continue
            primary = (inspector.get_pk_constraint(table, schema=schema) or {}).get(
                "constrained_columns"
            ) or []
            select = ["COUNT(*)"]
            if len(primary) == 1:
                select.append(f"MAX({_quote(str(primary[0]), dialect)})")
            qualified = _qualified(table, schema, dialect)
            try:
                with engine.connect() as connection:
                    row = connection.execute(
                        text(f"SELECT {', '.join(select)} FROM {qualified}")
                    ).first()
            except Exception as ex:  # pylint: disable=broad-except
                logger.warning("Could not read stats for %s: %s", table, ex)
                continue
            rows = int(row[0] or 0) if row is not None else 0
            maxid = None
            if len(primary) == 1 and row is not None and row[1] is not None:
                maxid = int(row[1])
            stats[table] = {"rows": rows, "maxid": maxid}
    return stats
