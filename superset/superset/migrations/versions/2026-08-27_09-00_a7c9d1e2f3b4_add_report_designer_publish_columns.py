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
"""Add publish metadata columns to report_designer

Revision ID: a7c9d1e2f3b4
Revises: c7d5e9f1a2b3
Create Date: 2026-08-27 09:00:00.000000

"""

from alembic import op
from sqlalchemy import Column, DateTime, Integer, String

# revision identifiers, used by Alembic.
revision = "a7c9d1e2f3b4"
down_revision = "c7d5e9f1a2b3"

TABLE = "report_designer"


def upgrade():
    """Add columns tracking the published chart/dashboard for a report."""
    op.add_column(TABLE, Column("dataset_id", Integer, nullable=True))
    op.add_column(TABLE, Column("chart_id", Integer, nullable=True))
    op.add_column(TABLE, Column("dashboard_id", Integer, nullable=True))
    op.add_column(TABLE, Column("viz_type", String(64), nullable=True))
    op.add_column(TABLE, Column("chart_name", String(256), nullable=True))
    op.add_column(TABLE, Column("published_at", DateTime, nullable=True))


def downgrade():
    """Drop the publish metadata columns."""
    op.drop_column(TABLE, "published_at")
    op.drop_column(TABLE, "chart_name")
    op.drop_column(TABLE, "viz_type")
    op.drop_column(TABLE, "dashboard_id")
    op.drop_column(TABLE, "chart_id")
    op.drop_column(TABLE, "dataset_id")
