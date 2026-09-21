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
"""SQL generation for the Report Designer.

Turns a report definition (datasets, relationships, fields, groupings,
aggregations and filters) into a SQLAlchemy ``Select`` statement, compiles it
against the underlying database and executes it through Superset's standard
``Database.get_df`` path (engine-spec aware, logged, mutated).
"""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd
from flask_babel import gettext as _
from sqlalchemy import false, func, literal_column, select, true
from sqlalchemy.sql.selectable import Select

from superset.connectors.sqla.models import SqlaTable
from superset.daos.dataset import DatasetDAO
from superset.models.core import Database

# Supported aggregation functions, mapped to SQLAlchemy funcs.
AGGREGATIONS: dict[str, Any] = {
    "SUM": func.sum,
    "AVG": func.avg,
    "COUNT": func.count,
    "COUNT_DISTINCT": lambda col: func.count(func.distinct(col)),
    "MIN": func.min,
    "MAX": func.max,
}

# Simple comparison filter operators.
COMPARISONS: dict[str, Any] = {
    "=": lambda col, value: col == value,
    "!=": lambda col, value: col != value,
    ">": lambda col, value: col > value,
    ">=": lambda col, value: col >= value,
    "<": lambda col, value: col < value,
    "<=": lambda col, value: col <= value,
}

# Supported join types.
JOIN_TYPES = {"INNER", "LEFT", "FULL"}


class ReportDesignerError(Exception):
    """Raised when a report definition cannot be turned into a query."""


def normalize_definition(definition: dict[str, Any]) -> dict[str, Any]:
    """Fill defaults and coerce types for a report definition."""
    return {
        "version": int(definition.get("version") or 1),
        "datasets": definition.get("datasets") or [],
        "relationships": definition.get("relationships") or [],
        "columns": definition.get("columns") or [],
        "metrics": definition.get("metrics") or [],
        "group_by": definition.get("group_by") or [],
        "filters": definition.get("filters") or [],
        "order_by": definition.get("order_by") or [],
        "limit": int(definition.get("limit") or 1000),
    }


def _load_datasets(
    definition: dict[str, Any],
) -> tuple[dict[int, SqlaTable], dict[int, str]]:
    """Load datasets for a report, enforcing access control.

    Returns a mapping of dataset id -> dataset and dataset id -> alias. Only
    datasets the current user can access are returned; anything else raises.
    """
    datasets: dict[int, SqlaTable] = {}
    aliases: dict[int, str] = {}
    for index, entry in enumerate(definition["datasets"]):
        ds_id = int(entry["id"])
        dataset = DatasetDAO.find_by_id(ds_id)
        if dataset is None:
            raise ReportDesignerError(
                _("Dataset %(id)s not found or not accessible", id=ds_id)
            )
        if dataset.is_virtual:
            raise ReportDesignerError(
                _(
                    "Dataset %(id)s is a virtual (SQL) dataset, which is not "
                    "supported by the Report Designer yet",
                    id=ds_id,
                )
            )
        datasets[ds_id] = dataset
        aliases[ds_id] = entry.get("alias") or f"d{index + 1}"
    if not datasets:
        raise ReportDesignerError(_("Report needs at least one dataset"))
    return datasets, aliases


def _column_expr(
    dataset: SqlaTable,
    table_alias: Any,
    column_name: str,
) -> Any:
    """Return a SQLAlchemy expression for a column.

    Physical columns are referenced on the aliased table; columns defined as
    SQL expressions fall back to the raw expression string.
    """
    column = dataset.get_column(column_name)
    if column is not None and column.expression:
        return literal_column(f"({column.expression})")
    return table_alias.c[column_name]


def _filter_expr(
    dataset: SqlaTable,
    table_alias: Any,
    filt: dict[str, Any],
) -> Any:
    """Build a SQLAlchemy filter expression from a filter definition."""
    op = str(filt.get("op") or "=").upper()
    column = _column_expr(dataset, table_alias, filt["column"])
    value = filt.get("value")

    if op == "IS NULL":
        return column.is_(None)
    if op == "IS NOT NULL":
        return column.is_not(None)
    if op == "IN":
        items = value if isinstance(value, list) else [value]
        return column.in_(items) if items else false()
    if op == "NOT IN":
        items = value if isinstance(value, list) else [value]
        return column.not_in(items) if items else true()
    if op == "BETWEEN":
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise ReportDesignerError(_("BETWEEN filter needs a [low, high] value"))
        return column.between(value[0], value[1])
    if op == "LIKE":
        return column.like(f"%{value}%")
    if op not in COMPARISONS:
        raise ReportDesignerError(_("Unsupported filter operator: %(op)s", op=op))
    return COMPARISONS[op](column, value)


def _apply_group_by(
    stmt: Select,
    definition: dict[str, Any],
    datasets: dict[int, SqlaTable],
    tables: dict[int, Any],
) -> Select:
    """Apply GROUP BY — required whenever metrics are present."""
    if not definition["metrics"]:
        return stmt
    group_by = definition["group_by"]
    if not group_by:
        group_by = [
            {"dataset": c["dataset"], "column": c["column"]}
            for c in definition["columns"]
        ]
    exprs = [
        _column_expr(
            datasets[int(entry["dataset"])],
            tables[int(entry["dataset"])],
            entry["column"],
        )
        for entry in group_by
    ]
    return stmt.group_by(*exprs)


def _apply_filters(
    stmt: Select,
    definition: dict[str, Any],
    datasets: dict[int, SqlaTable],
    tables: dict[int, Any],
) -> Select:
    """Apply report filters to a statement."""
    exprs = [
        _filter_expr(
            datasets[int(entry["dataset"])],
            tables[int(entry["dataset"])],
            entry,
        )
        for entry in definition["filters"]
    ]
    return stmt.where(*exprs) if exprs else stmt


def _apply_order_by(
    stmt: Select,
    definition: dict[str, Any],
    datasets: dict[int, SqlaTable],
    tables: dict[int, Any],
    label_map: dict[str, Any],
) -> Select:
    """Apply ordering to a statement."""
    for entry in definition["order_by"]:
        ds_id = int(entry["dataset"])
        column_name = entry.get("column")
        label = entry.get("label")
        if label and label in label_map:
            expr = label_map[label]
        else:
            expr = _column_expr(datasets[ds_id], tables[ds_id], column_name)
        stmt = stmt.order_by(expr.desc() if entry.get("desc") else expr.asc())
    return stmt


def build_select(definition: dict[str, Any]) -> tuple[Select, SqlaTable]:
    """Build a SQLAlchemy ``Select`` for a report definition.

    Returns the compiled statement together with the primary dataset (used to
    resolve the database that should execute the query).
    """
    definition = normalize_definition(definition)
    datasets, aliases = _load_datasets(definition)
    tables = {
        ds_id: dataset.get_sqla_table_object().alias(aliases[ds_id])
        for ds_id, dataset in datasets.items()
    }

    select_cols: list[Any] = []
    label_map: dict[str, Any] = {}

    # Plain (dimension) columns.
    for entry in definition["columns"]:
        ds_id = int(entry["dataset"])
        expr = _column_expr(datasets[ds_id], tables[ds_id], entry["column"])
        label = entry.get("label") or entry["column"]
        select_cols.append(expr.label(label))
        label_map[label] = expr

    # Aggregated metrics.
    for entry in definition["metrics"]:
        ds_id = int(entry["dataset"])
        aggregate = str(entry.get("aggregate") or "SUM").upper()
        if aggregate not in AGGREGATIONS:
            raise ReportDesignerError(
                _("Unsupported aggregation: %(agg)s", agg=aggregate)
            )
        expr = AGGREGATIONS[aggregate](
            _column_expr(datasets[ds_id], tables[ds_id], entry["column"])
        )
        label = entry.get("label") or f"{aggregate}({entry['column']})"
        select_cols.append(expr.label(label))
        label_map[label] = expr

    if not select_cols:
        raise ReportDesignerError(_("Report needs at least one field or metric"))

    # FROM + JOINs, starting from the first dataset.
    primary_id = int(definition["datasets"][0]["id"])
    stmt = select(*select_cols).select_from(tables[primary_id])

    for rel in definition["relationships"]:
        left = tables[int(rel["left_dataset"])]
        right = tables[int(rel["right_dataset"])]
        onclause = left.c[rel["left_column"]] == right.c[rel["right_column"]]
        join_type = str(rel.get("join_type") or "INNER").upper()
        if join_type not in JOIN_TYPES:
            raise ReportDesignerError(
                _("Unsupported join type: %(jt)s", jt=join_type)
            )
        if join_type == "INNER":
            stmt = stmt.join(right, onclause)
        elif join_type == "LEFT":
            stmt = stmt.join(right, onclause, isouter=True)
        else:
            stmt = stmt.join(right, onclause, full=True)

    stmt = _apply_group_by(stmt, definition, datasets, tables)
    stmt = _apply_filters(stmt, definition, datasets, tables)
    stmt = _apply_order_by(stmt, definition, datasets, tables, label_map)
    stmt = stmt.limit(definition["limit"])

    return stmt, datasets[primary_id]


def compile_sql(stmt: Select, database: Database) -> str:
    """Compile a statement to a SQL string with safe literal binds."""
    return database.compile_sqla_query(stmt)


def execute_report(
    definition: dict[str, Any],
) -> dict[str, Any]:
    """Execute a report definition and return rows/columns for preview.

    Returns: {"sql", "columns", "rows", "count", "truncated"}
    """
    definition = normalize_definition(definition)
    stmt, primary = build_select(definition)
    database = primary.database

    sql = compile_sql(stmt, database)
    df = database.get_df(sql)

    columns = [str(col) for col in df.columns]
    rows = [_json_safe_row(row) for row in df.to_dict(orient="records")]

    return {
        "sql": sql,
        "columns": columns,
        "rows": rows,
        "count": len(rows),
        "truncated": len(rows) >= definition["limit"],
    }


def _json_safe_row(row: dict[str, Any]) -> dict[str, Any]:
    """Convert numpy/pandas/timestamp values to JSON-safe Python values."""
    return {key: _json_safe(value) for key, value in row.items()}


def _json_safe(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        if math.isnan(value) or math.isinf(value):
            return None
        return float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, (np.ndarray, list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()
    # pd.NA is a singleton (NAType), not a type — compare by identity.
    if value is pd.NA or (
        isinstance(value, float) and (math.isnan(value) or math.isinf(value))
    ):
        return None
    return value


def datasets_payload() -> list[dict[str, Any]]:
    """List datasets accessible to the current user for the report picker."""
    from superset.connectors.sqla.models import SqlMetric

    payload: list[dict[str, Any]] = []
    for dataset in DatasetDAO.find_all():
        if dataset.is_virtual:
            continue
        columns = []
        for column in dataset.columns:
            columns.append(
                {
                    "column_name": column.column_name,
                    "type": column.type,
                    "is_dttm": column.is_dttm,
                    "expression": column.expression,
                }
            )
        metrics = []
        for metric in dataset.metrics:
            if isinstance(metric, SqlMetric):
                metrics.append(
                    {
                        "metric_name": metric.metric_name,
                        "expression": metric.expression,
                    }
                )
        payload.append(
            {
                "id": dataset.id,
                "table_name": dataset.table_name,
                "schema": dataset.schema,
                "catalog": dataset.catalog,
                "database_name": dataset.database.database_name,
                "columns": columns,
                "metrics": metrics,
            }
        )
    return payload
