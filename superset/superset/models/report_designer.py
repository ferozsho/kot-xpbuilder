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
"""Report Designer model.

Stores report definitions (a JSON document describing the datasets,
relationships, fields, groupings, aggregations and filters of a
Crystal-Reports-style multi-table report). The actual query execution is
delegated to the underlying Superset datasets/databases.
"""

from __future__ import annotations

from typing import Any

from flask_appbuilder import Model
from sqlalchemy import Column, DateTime, Integer, String, Text

from superset.models.helpers import AuditMixinNullable
from superset.utils import json


class ReportDesigner(AuditMixinNullable, Model):
    """ORM object for saved report designer definitions."""

    __tablename__ = "report_designer"

    id = Column(Integer, primary_key=True)
    name = Column(String(256), nullable=False)
    description = Column(Text, nullable=True)
    # Full report definition as a JSON document:
    # {
    #   "version": 1,
    #   "datasets": [{"id": 1, "alias": "d1"}, ...],
    #   "relationships": [{"left_dataset": 1, "left_column": "x",
    #                      "right_dataset": 2, "right_column": "y",
    #                      "join_type": "INNER"}, ...],
    #   "columns": [{"dataset": 1, "column": "customer_name",
    #                "label": "Customer Name"}, ...],
    #   "metrics": [{"dataset": 2, "column": "amount",
    #                "aggregate": "SUM", "label": "Total Amount"}, ...],
    #   "group_by": [{"dataset": 1, "column": "customer_name"}, ...],
    #   "filters": [{"dataset": 2, "column": "order_date", "op": ">=",
    #                "value": "2026-01-01"}, ...],
    #   "order_by": [{"dataset": 1, "column": "customer_name", "desc": false}],
    #   "limit": 1000
    # }
    definition = Column(Text, nullable=False, default="{}")

    # Publish metadata — set when the report is published to Superset as a
    # chart attached to a dashboard.
    dataset_id = Column(Integer, nullable=True)
    chart_id = Column(Integer, nullable=True)
    dashboard_id = Column(Integer, nullable=True)
    viz_type = Column(String(64), nullable=True)
    chart_name = Column(String(256), nullable=True)
    published_at = Column(DateTime, nullable=True)

    export_fields = [
        "id",
        "name",
        "description",
        "definition",
        "dataset_id",
        "chart_id",
        "dashboard_id",
        "viz_type",
        "chart_name",
        "published_at",
    ]

    def get_definition(self) -> dict[str, Any]:
        """Return the definition as a parsed dictionary."""
        try:
            return json.loads(self.definition or "{}")
        except (TypeError, ValueError):
            return {}

    def set_definition(self, definition: dict[str, Any]) -> None:
        """Persist the definition as a JSON string."""
        self.definition = json.dumps(definition, default=str)

    def to_dict(self) -> dict[str, Any]:
        """Serialize for the API response."""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description or "",
            "definition": self.get_definition(),
            "dataset_id": self.dataset_id,
            "chart_id": self.chart_id,
            "dashboard_id": self.dashboard_id,
            "viz_type": self.viz_type,
            "chart_name": self.chart_name,
            "published_at": self.published_at.isoformat()
            if self.published_at
            else None,
            "changed_on": self.changed_on.isoformat() if self.changed_on else None,
            "created_on": self.created_on.isoformat() if self.created_on else None,
        }
