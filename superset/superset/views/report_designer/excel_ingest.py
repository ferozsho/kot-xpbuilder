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
) -> dict[str, Any]:
    """Read an uploaded spreadsheet, stage it as a table, register a dataset.

    The target database must be writable (the report's read replica is not).
    Returns metadata about the new dataset.
    """
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
            df.to_sql(
                clean,
                con=engine,
                if_exists="replace",
                index=False,
                method="multi",
            )
    except Exception as ex:
        raise IngestError(
            _("Could not write staging table (is the database writable?): %(err)s",
              err=ex)
        ) from ex

    # Register as a Superset dataset (physical table).
    dataset = SqlaTable(
        database_id=database.id,
        schema=schema,
        table_name=clean,
    )
    db.session.add(dataset)
    db.session.commit()

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
