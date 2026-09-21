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
"""Create report_designer table for the Report Designer feature

Revision ID: c7d5e9f1a2b3
Revises: 4b2a8c9d3e1f
Create Date: 2026-08-26 14:00:00.000000

"""

from sqlalchemy import Column, DateTime, Integer, String, Text

from superset.migrations.shared.utils import (
    create_fks_for_table,
    create_index,
    create_table,
    drop_fks_for_table,
    drop_index,
    drop_table,
)

# revision identifiers, used by Alembic.
revision = "c7d5e9f1a2b3"
down_revision = "4b2a8c9d3e1f"

REPORT_DESIGNER_TABLE = "report_designer"


def upgrade():
    """
    Create the report_designer table.

    Stores Crystal-Reports-style report definitions (datasets, relationships,
    fields, groupings, aggregations, filters) as a JSON document.
    """
    create_table(
        REPORT_DESIGNER_TABLE,
        Column("id", Integer, primary_key=True),
        Column("name", String(256), nullable=False),
        Column("description", Text, nullable=True),
        Column("definition", Text, nullable=False, server_default="{}"),
        # AuditMixinNullable columns
        Column("created_on", DateTime, nullable=True),
        Column("changed_on", DateTime, nullable=True),
        Column("created_by_fk", Integer, nullable=True),
        Column("changed_by_fk", Integer, nullable=True),
    )

    create_index(REPORT_DESIGNER_TABLE, "idx_report_designer_name", ["name"])
    create_index(
        REPORT_DESIGNER_TABLE,
        "idx_report_designer_created_on",
        ["created_on"],
    )
    create_index(
        REPORT_DESIGNER_TABLE,
        "idx_report_designer_changed_on",
        ["changed_on"],
    )
    create_index(
        REPORT_DESIGNER_TABLE,
        "idx_report_designer_created_by",
        ["created_by_fk"],
    )

    create_fks_for_table(
        foreign_key_name="fk_report_designer_created_by_fk_ab_user",
        table_name=REPORT_DESIGNER_TABLE,
        referenced_table="ab_user",
        local_cols=["created_by_fk"],
        remote_cols=["id"],
        ondelete="SET NULL",
    )

    create_fks_for_table(
        foreign_key_name="fk_report_designer_changed_by_fk_ab_user",
        table_name=REPORT_DESIGNER_TABLE,
        referenced_table="ab_user",
        local_cols=["changed_by_fk"],
        remote_cols=["id"],
        ondelete="SET NULL",
    )


def downgrade():
    """Drop the report_designer table."""
    drop_index(REPORT_DESIGNER_TABLE, "idx_report_designer_name")
    drop_index(REPORT_DESIGNER_TABLE, "idx_report_designer_created_on")
    drop_index(REPORT_DESIGNER_TABLE, "idx_report_designer_changed_on")
    drop_index(REPORT_DESIGNER_TABLE, "idx_report_designer_created_by")
    drop_fks_for_table(REPORT_DESIGNER_TABLE)
    drop_table(REPORT_DESIGNER_TABLE)
