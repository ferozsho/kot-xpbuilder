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
"""Excel/CSV ingestion for the Report Designer.

Turns an uploaded spreadsheet into a staging table in a writable database and
registers it as a Superset dataset so it can be used in reports. This is the
"Data Modeler" ingestion path described in the Report Designer architecture.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

from flask_babel import gettext as _
from sqlalchemy.exc import IntegrityError

from superset import db
from superset.connectors.sqla.models import SqlaTable
from superset.models.core import Database

logger = logging.getLogger(__name__)

IMPORT_ERROR_MSG = _(
    "Excel ingestion requires pandas + openpyxl (installed in the runtime image)"
)


class IngestError(Exception):
    """Raised when an uploaded file cannot be turned into a dataset."""


def _normalize_column_name(name: str, existing: set[str]) -> str:
    """Turn an arbitrary header into a safe DB identifier."""
    cleaned = re.sub(r"[^a-z0-9_]", "_", str(name).strip().lower())
    if not cleaned or not cleaned[0].isalpha():
        cleaned = f"col_{cleaned}" if cleaned else "col"
    cleaned = cleaned[:60].rstrip("_")
    while cleaned in existing:
        cleaned = f"{cleaned}_"
    existing.add(cleaned)
    return cleaned


def _staging_dtypes(df, engine) -> dict:
    """Column types for the staging table.

    Moodle HTML/plain-text columns (``assignsubmission_onlinetext.onlinetext``,
    forum posts, page content) routinely exceed MySQL's TEXT ceiling of 64 KB,
    and pandas' default inference picked a type that rejected them with
    "Data too long for column". Every textual column is therefore created as the
    widest text type the target dialect offers.
    """
    from pandas.api.types import is_object_dtype, is_string_dtype

    dialect = (engine.dialect.name or "").lower()
    if dialect in ("mysql", "mariadb"):
        from sqlalchemy.dialects.mysql import LONGTEXT

        text_type = LONGTEXT()
    elif dialect == "postgresql":
        from sqlalchemy import Text

        text_type = Text()
    else:
        from sqlalchemy import Text

        text_type = Text()

    dtypes = {}
    for column in df.columns:
        series = df[column]
        if is_object_dtype(series) or is_string_dtype(series):
            dtypes[column] = text_type
    return dtypes


def _blank_text(series, keep_null: bool):
    """What an empty text cell becomes: NULL for a nullable key, else ""."""
    if keep_null:
        blank = series.isna() | (series.astype(str).str.strip() == "")
        return series.where(~blank, None)
    return series.where(series.notna(), "").astype(str)


def _align_to_existing(df, engine, table: str, schema: str | None):
    """Cast appended data to the column types of the table already created.

    The table now comes from the source's own definition (see
    :mod:`table_schema`), so every part has to match real Moodle column types
    rather than the LONGTEXT staging types pandas would have created:

    * numeric columns are coerced (an unparsable value becomes NULL instead of
      "Data truncated for column"),
    * date/time columns are parsed, and an empty cell becomes NULL instead of
      the empty string MySQL rejects under strict mode,
    * text columns keep an empty string, because that is what Moodle stores,
      except when the column is nullable and part of a UNIQUE index: there the
      source's NULL has to come back as NULL, because MySQL counts two NULLs as
      distinct but two empty strings as a duplicate. Moodle's question bank
      (``question_bank_entries.idnumber``), ``context.idnumber`` and the
      competency tables all rely on that, and collapsing them to "" made every
      one of their rows collide with the next one.
    """
    import pandas as pd
    from sqlalchemy import inspect

    numeric_prefixes = (
        "INT",
        "BIGINT",
        "SMALLINT",
        "TINYINT",
        "MEDIUMINT",
        "DECIMAL",
        "NUMERIC",
        "FLOAT",
        "DOUBLE",
        "REAL",
        "BIT",
    )
    temporal_prefixes = ("DATE", "DATETIME", "TIMESTAMP", "TIME", "YEAR")
    try:
        columns = inspect(engine).get_columns(table, schema=schema)
    except Exception:  # pylint: disable=broad-except
        return df

    # Columns where an empty cell must not become an empty string (see above).
    unique_columns: set[str] = set()
    try:
        for index in inspect(engine).get_indexes(table, schema=schema):
            if index.get("unique"):
                unique_columns.update(index.get("column_names") or [])
    except Exception:  # pylint: disable=broad-except
        unique_columns = set()

    for column in columns:
        name = column["name"]
        if name not in df.columns:
            continue
        sqltype = str(column["type"]).upper()
        if sqltype.startswith(numeric_prefixes):
            df[name] = pd.to_numeric(df[name], errors="coerce")
        elif sqltype.startswith(temporal_prefixes):
            df[name] = pd.to_datetime(df[name], errors="coerce")
        elif sqltype.startswith(("CHAR", "VARCHAR", "TEXT", "TINYTEXT", "MEDIUMTEXT",
                                 "LONGTEXT")):
            keepnull = bool(column.get("nullable")) and name in unique_columns
            df[name] = _blank_text(df[name], keepnull)
        else:
            # enum/set/json/binary and anything else: a literal empty string is
            # not a legal value, so an empty cell has to stay NULL.
            df[name] = df[name].where(df[name].notna(), None)
    return df


def _read_dataframe(file_stream: Any, filename: str):
    try:
        import pandas as pd
    except ImportError as ex:  # pragma: no cover
        raise IngestError(str(IMPORT_ERROR_MSG)) from ex

    name = (filename or "").lower()
    try:
        if name.endswith(".csv"):
            return pd.read_csv(file_stream)
        return pd.read_excel(file_stream)
    except Exception as ex:
        raise IngestError(_("Could not read the spreadsheet: %(err)s", err=ex)) from ex


def ingest_excel(
    database: Database,
    file_stream: Any,
    filename: str,
    table_name: str | None = None,
    schema: str | None = None,
    mode: str = "replace",
) -> dict[str, Any]:
    """Read an uploaded spreadsheet, stage it as a table, register a dataset.

    The target database must be writable (the report's read replica is not).

    ``mode`` is ``replace`` for a whole file (the historic behaviour) or
    ``append`` when a large file is pushed in parts: the first part replaces the
    table, later parts append to it. Edge proxies reject big bodies (Cloudflare
    answers 413 above ~100 MB), so the caller splits large files itself.

    Returns metadata about the new dataset.
    """
    append = str(mode).lower() == "append"
    df = _read_dataframe(file_stream, filename)
    if df is None or df.empty:
        raise IngestError(_("The uploaded file has no rows"))

    existing: set[str] = set()
    columns = [_normalize_column_name(c, existing) for c in df.columns]
    df.columns = columns

    if table_name:
        clean = re.sub(r"[^a-z0-9_]", "_", table_name.lower())
        if not clean[0].isalpha():
            clean = f"xp_{clean}"
    else:
        clean = f"xp_upload_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    try:
        with database.get_sqla_engine(schema=schema) as engine:
            # Wide text columns plus row-sized chunks: a single INSERT can then
            # neither overflow a column nor exceed max_allowed_packet, which is
            # what made a whole table fail on one oversized row.
            if append:
                df = _align_to_existing(df, engine, clean, schema)
            df.to_sql(
                clean,
                con=engine,
                if_exists="append" if append else "replace",
                index=False,
                dtype=None if append else _staging_dtypes(df, engine),
                chunksize=500,
            )
    except Exception as ex:
        raise IngestError(
            _("Could not write staging table (is the database writable?): %(err)s",
              err=ex)
        ) from ex

    # Register as a Superset dataset (physical table).
    #
    # The (database_id, schema, table_name) triple is unique, and a previous
    # upload of the same table may already have registered it (a run that was
    # interrupted after staging, or the Moodle sync re-sending a table). Insert
    # is therefore attempted only when the row is absent, and a concurrent
    # insert is adopted instead of surfacing a 400 to the client.
    def find_dataset() -> SqlaTable | None:
        return (
            db.session.query(SqlaTable)
            .filter_by(database_id=database.id, schema=schema, table_name=clean)
            .one_or_none()
        )

    dataset = find_dataset()
    if append and dataset is not None:
        # A later part: the table and dataset already exist, only the rows were
        # appended above.
        try:
            dataset.fetch_metadata()
        except Exception:  # pylint: disable=broad-except
            logger.warning("Metadata fetch failed for appended dataset %s", clean)
        db.session.commit()
        return {
            "dataset_id": dataset.id,
            "table_name": clean,
            "rows": int(len(df)),
            "columns": columns,
            "mode": "append",
        }
    if dataset is None:
        dataset = SqlaTable(
            database_id=database.id,
            schema=schema,
            table_name=clean,
        )
        db.session.add(dataset)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            dataset = find_dataset()
            if dataset is None:
                raise
    else:
        logger.info("Reusing existing dataset %s.%s", schema, clean)

    try:
        dataset.fetch_metadata()
    except Exception:  # pylint: disable=broad-except
        # Metadata fetch is best-effort; columns can be refreshed later.
        logger.warning("Metadata fetch failed for staged dataset %s", clean)
    db.session.commit()

    return {
        "dataset_id": dataset.id,
        "table_name": clean,
        "rows": int(len(df)),
        "columns": columns,
    }
