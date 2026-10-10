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
"""Periodic sync of the reporting database (Moodle) into Superset datasets.

The Report Designer's "Sync Moodle tables" button and the
``superset sync-moodle-tables`` CLI command do the same work on demand. This
task keeps the dataset list in step with the source schema without anybody
pressing a button; it is scheduled by the ``xpbuilder-sync-moodle-tables``
entry that ``config/superset_config.py`` adds to
``XpBuilderCeleryConfig.beat_schedule`` when ``XPBUILDER_MOODLE_INTEGRATION``
is enabled.

The operation is idempotent: ``SqlaTable`` carries a unique constraint on
(database_id, catalog, schema, table_name), so a table that is already
registered is skipped rather than duplicated.
"""

from __future__ import annotations

import logging
from typing import Any

from superset.extensions import celery_app
from superset.views.report_designer.table_sync import (
    find_reporting_database,
    sync_tables,
    TableSyncError,
)

logger = logging.getLogger(__name__)


@celery_app.task(
    name="xpbuilder.sync_moodle_tables",
    soft_time_limit=1800,
    time_limit=2100,
)
def sync_moodle_tables() -> dict[str, Any]:
    """Register every table of the reporting database as a Superset dataset.

    :returns: the summary produced by ``sync_tables`` (or a ``skipped`` marker
        when no reporting database is configured / the source is unreachable).
    """
    database = find_reporting_database()
    if database is None:
        logger.info("Moodle table sync skipped: no reporting database configured")
        return {"skipped": True, "reason": "no reporting database configured"}

    try:
        result = sync_tables(database.id)
    except TableSyncError as ex:
        # A reporting database that is unreachable (network blip, credentials
        # rotated) must not fail the worker: the next run retries.
        logger.warning("Moodle table sync failed: %s", ex)
        return {"skipped": True, "reason": str(ex)}

    logger.info(
        "Moodle table sync: %s table(s) on '%s' (%s new, %s existing, %s failed)",
        result["total"],
        result["database_name"],
        result["created"],
        result["skipped"],
        len(result["failed"]),
    )
    return result
