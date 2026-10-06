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
"""Sync physical tables on a reporting database into Superset datasets.

Enumerates every table on a database (e.g. the read-only "Moodle Reporting"
replica) and registers the missing ones as physical Superset datasets so the
Report Designer lists all of them by default.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from flask_babel import gettext as _

from superset import db
from superset.connectors.sqla.models import SqlaTable
from superset.models.core import Database

logger = logging.getLogger(__name__)

#: Database used when the caller does not pass an explicit ``database_id``.
#: Overridable per site via XPBUILDER_REPORTING_DATABASE, because the read-only
#: reporting database is named differently on each deployment. The Report
#: Designer frontend normally sends the selected database id, so this is only
#: the fallback used by `superset sync-moodle-tables`.
DEFAULT_REPORTING_DATABASE = os.environ.get(
    "XPBUILDER_REPORTING_DATABASE", "Moodle Reporting"
)


class TableSyncError(Exception):
    """Raised when the table sync cannot complete."""


def find_reporting_database(
    name: str = DEFAULT_REPORTING_DATABASE,
) -> Database | None:
    """Return the read-only reporting database by name, if configured."""
    return (
        db.session.query(Database)
        .filter(Database.database_name == name)
        .first()
    )


def _existing_table_names(database_id: int) -> set[str]:
    """Return table names already registered as physical datasets."""
    rows = (
        db.session.query(SqlaTable.table_name)
        .filter(
            SqlaTable.database_id == database_id,
            SqlaTable.sql.is_(None),
        )
        .all()
    )
    return {row[0] for row in rows}


def _list_tables(database: Database) -> set[str]:
    """Enumerate tables from the engine, bypassing the memoized table cache.

    ``Database.get_all_table_names_in_schema`` is decorated with a cache keyed
    on ``db:{id}:...:table_list``; the sync must see freshly created tables,
    so we query the inspector directly.
    """
    try:
        with database.get_inspector(catalog=None, schema=None) as inspector:
            names = database.db_engine_spec.get_table_names(
                database=database,
                inspector=inspector,
                schema=None,
            )
    except Exception as ex:  # pylint: disable=broad-except
        raise TableSyncError(
            _(
                "Could not list tables on %(name)s: %(err)s",
                name=database.database_name,
                err=ex,
            )
        ) from ex
    return set(names)


def sync_tables(database_id: int) -> dict[str, Any]:
    """Register every table on a database as a physical Superset dataset.

    Idempotent — tables already registered are skipped. Column metadata is
    fetched best-effort per table so they are immediately usable in the
    Report Designer (drag & drop columns).

    Returns a summary dict with total/created/skipped/failed counts.
    """
    database = db.session.get(Database, database_id)
    if database is None:
        raise TableSyncError(_("Database %(id)s not found", id=database_id))

    table_names = _list_tables(database)
    existing = _existing_table_names(database_id)

    created: list[str] = []
    failed: list[dict[str, str]] = []
    skipped = 0

    for name in sorted(table_names):
        if name in existing:
            skipped += 1
            continue
        dataset = SqlaTable(
            database_id=database_id,
            schema=None,
            table_name=name,
        )
        db.session.add(dataset)
        try:
            db.session.commit()
            try:
                dataset.fetch_metadata()
                db.session.commit()
            except Exception:  # pylint: disable=broad-except
                # Columns can be refreshed later; the registration itself is
                # already committed, so keep going.
                logger.warning("Metadata fetch failed for %s", name)
                db.session.rollback()
            created.append(name)
            existing.add(name)
        except Exception as ex:  # pylint: disable=broad-except
            db.session.rollback()
            failed.append({"table": name, "error": str(ex)})
            logger.warning("Failed to register table %s: %s", name, ex)

    return {
        "database_id": database_id,
        "database_name": database.database_name,
        "total": len(table_names),
        "created": len(created),
        "skipped": skipped,
        "failed": failed,
        "tables": created,
    }
