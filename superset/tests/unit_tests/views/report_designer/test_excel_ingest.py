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
"""Unit tests for Report Designer spreadsheet ingestion."""

from contextlib import nullcontext
from unittest import mock

import pandas as pd

from superset.views.report_designer import excel_ingest


def test_append_preserves_the_requested_postgres_schema() -> None:
    frame = pd.DataFrame({"id": [1], "name": ["Admin"]})
    engine = mock.MagicMock()
    engine.dialect.name = "postgresql"
    database = mock.MagicMock(id=1)
    database.get_sqla_engine.return_value = nullcontext(engine)
    dataset = mock.MagicMock(id=42)

    with (
        mock.patch.object(excel_ingest, "_read_dataframe", return_value=frame),
        mock.patch.object(excel_ingest, "_align_to_existing", return_value=frame),
        mock.patch.object(frame, "to_sql") as to_sql,
        mock.patch.object(excel_ingest.db, "session") as session,
    ):
        session.query.return_value.filter_by.return_value.one_or_none.return_value = (
            dataset
        )
        result = excel_ingest.ingest_excel(
            database,
            mock.MagicMock(),
            "mdl_user.csv",
            table_name="mdl_user",
            schema="auditonekef",
            mode="append",
        )

    assert to_sql.call_args.kwargs["schema"] == "auditonekef"
    assert result["dataset_id"] == 42
    assert result["mode"] == "append"
