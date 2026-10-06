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
"""Register all tables of the read-only Moodle Reporting database as datasets.

Runs automatically during stack initialization (``docker/initialize.sh``) and
on demand via the Report Designer's "Sync Moodle tables" button.
"""

from __future__ import annotations

import click
from flask.cli import with_appcontext


@click.command()
@with_appcontext
def sync_moodle_tables() -> None:
    """Register every table of the Moodle Reporting database as a dataset."""
    # Imported lazily to avoid pulling the report designer view stack into the
    # CLI import graph at command-discovery time.
    from superset import db
    from superset.views.report_designer.table_sync import (
        find_reporting_database,
        sync_tables,
        TableSyncError,
    )

    database = find_reporting_database()
    if database is None:
        raise click.ClickException(
            "Moodle Reporting database not found. Run `superset "
            "set-database-uri -d 'Moodle Reporting' -u <uri>` first."
        )
    try:
        result = sync_tables(database.id)
    except TableSyncError as ex:
        raise click.ClickException(str(ex)) from ex

    click.echo(
        f"Synced {result['database_name']}: "
        f"{result['created']} new, {result['skipped']} existing, "
        f"{len(result['failed'])} failed (total {result['total']} tables)."
    )
    for failure in result["failed"]:
        click.echo(f"  FAILED {failure['table']}: {failure['error']}")
    if result["failed"]:
        raise click.ClickException(
            f"{len(result['failed'])} tables failed to register."
        )

    # Keep the flask-SQLAlchemy session tidy after the CLI run.
    db.session.remove()
