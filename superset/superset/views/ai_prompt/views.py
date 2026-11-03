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
"""XP Prompt workspace: page plus an authorized proxy to the conversation API.

Superset stays the only place that answers "who is this user". The React
workspace never sends an identity: this view injects ``superset_user_id`` from
the Flask-AppBuilder session, so a caller cannot read or extend another user's
conversation by guessing an id or editing the request body.

The upstream call reuses :func:`superset.views.ai_provider.views.call_xp_prompt`,
which derives the internal service token from the root secret. Keeping that
helper in the AI Provider module means the salt used by both proxies is defined
once, and the compatibility contract keeps asserting it against the service.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from flask import g, request
from flask_appbuilder import expose
from flask_appbuilder.security.decorators import has_access, has_access_api
from flask_babel import gettext as _

from superset import db, security_manager
from superset.exceptions import SupersetSecurityException
from superset.models.core import Database
from superset.superset_typing import FlaskResponse
from superset.views.ai_provider.views import call_xp_prompt
from superset.views.base import BaseSupersetView

logger = logging.getLogger(__name__)


class AIPromptView(BaseSupersetView):
    """Conversational XP Prompt workspace."""

    route_base = "/aiprompt"
    class_permission_name = "AIPrompt"

    @expose("/")
    @has_access
    def workspace(self) -> FlaskResponse:
        return super().render_app_template()

    @expose("/list/")
    @has_access
    def list(self) -> FlaskResponse:  # noqa: A003 - FAB route name
        """Alias so an older bookmark or menu href still resolves."""
        return super().render_app_template()

    # --- Proxy API -----------------------------------------------------

    @expose("/api/conversations/", methods=("GET",))
    @has_access_api
    def api_conversations(self) -> FlaskResponse:
        owner = _current_user_id()
        if owner is None:
            return self.json_response(
                {"message": _("No authenticated user"), "severity": "danger"}, 401
            )
        status, body = call_xp_prompt(
            "GET", f"/ai/conversations?superset_user_id={owner}"
        )
        return self.json_response(body, status)

    @expose("/api/conversations/", methods=("POST",))
    @has_access_api
    def api_conversation_create(self) -> FlaskResponse:
        owner = _current_user_id()
        if owner is None:
            return self.json_response(
                {"message": _("No authenticated user"), "severity": "danger"}, 401
            )
        payload = _body()
        payload["superset_user_id"] = owner
        status, body = call_xp_prompt("POST", "/ai/conversations", payload=payload)
        return self.json_response(body, status)

    @expose("/api/conversations/<conversation_id>/", methods=("GET",))
    @has_access_api
    def api_conversation_detail(self, conversation_id: str) -> FlaskResponse:
        owner = _current_user_id()
        if owner is None:
            return self.json_response(
                {"message": _("No authenticated user"), "severity": "danger"}, 401
            )
        status, body = call_xp_prompt(
            "GET", f"/ai/conversations/{conversation_id}?superset_user_id={owner}"
        )
        return self.json_response(body, status)

    @expose("/api/conversations/<conversation_id>/messages/", methods=("POST",))
    @has_access_api
    def api_conversation_message(self, conversation_id: str) -> FlaskResponse:
        owner = _current_user_id()
        if owner is None:
            return self.json_response(
                {"message": _("No authenticated user"), "severity": "danger"}, 401
            )
        payload = _body()
        payload["superset_user_id"] = owner
        status, body = call_xp_prompt(
            "POST", f"/ai/conversations/{conversation_id}/messages", payload=payload
        )
        return self.json_response(body, status)

    @expose("/api/conversations/<conversation_id>/reset-context/", methods=("POST",))
    @has_access_api
    def api_conversation_reset_context(self, conversation_id: str) -> FlaskResponse:
        """Start a fresh context generation, keeping the visible transcript."""
        owner = _current_user_id()
        if owner is None:
            return self.json_response(
                {"message": _("No authenticated user"), "severity": "danger"}, 401
            )
        payload = _body()
        payload["superset_user_id"] = owner
        status, body = call_xp_prompt(
            "POST",
            f"/ai/conversations/{conversation_id}/context/reset",
            payload=payload,
        )
        return self.json_response(body, status)

    @expose("/api/conversations/<conversation_id>/archive/", methods=("POST",))
    @has_access_api
    def api_conversation_archive(self, conversation_id: str) -> FlaskResponse:
        owner = _current_user_id()
        if owner is None:
            return self.json_response(
                {"message": _("No authenticated user"), "severity": "danger"}, 401
            )
        status, body = call_xp_prompt(
            "POST",
            f"/ai/conversations/{conversation_id}/archive",
            payload={"superset_user_id": owner},
        )
        return self.json_response(body, status)

    # --- Jobs (P2-F05/F08) ---------------------------------------------

    @expose("/api/conversations/<conversation_id>/jobs/", methods=("GET", "POST"))
    @has_access_api
    def api_conversation_jobs(self, conversation_id: str) -> FlaskResponse:
        owner = _current_user_id()
        if owner is None:
            return self.json_response(
                {"message": _("No authenticated user"), "severity": "danger"}, 401
            )
        if request.method == "GET":
            status, body = call_xp_prompt(
                "GET",
                f"/ai/jobs?conversation_id={conversation_id}"
                f"&superset_user_id={owner}",
            )
            return self.json_response(body, status)
        payload = _body()
        payload["superset_user_id"] = owner
        payload["conversation_id"] = conversation_id
        status, body = call_xp_prompt("POST", "/ai/jobs", payload=payload)
        return self.json_response(body, status)

    @expose("/api/jobs/<job_id>/", methods=("GET",))
    @has_access_api
    def api_job_detail(self, job_id: str) -> FlaskResponse:
        owner = _current_user_id()
        if owner is None:
            return self.json_response(
                {"message": _("No authenticated user"), "severity": "danger"}, 401
            )
        status, body = call_xp_prompt(
            "GET", f"/ai/jobs/{job_id}?superset_user_id={owner}"
        )
        return self.json_response(body, status)

    @expose(
        "/api/jobs/<job_id>/<action>/",
        methods=("POST",),
    )
    @has_access_api
    def api_job_action(self, job_id: str, action: str) -> FlaskResponse:
        """Proxy cancel / retry / confirm / reject without inventing new verbs."""
        if action not in _JOB_ACTIONS:
            return self.json_response(
                {"message": _("Unsupported job action"), "severity": "danger"}, 404
            )
        owner = _current_user_id()
        if owner is None:
            return self.json_response(
                {"message": _("No authenticated user"), "severity": "danger"}, 401
            )
        payload = _body()
        payload["superset_user_id"] = owner
        status, body = call_xp_prompt(
            "POST", f"/ai/jobs/{job_id}/{_JOB_ACTIONS[action]}", payload=payload
        )
        return self.json_response(body, status)

    # --- Artifacts (P2-F07) --------------------------------------------

    @expose("/api/conversations/<conversation_id>/artifacts/", methods=("GET",))
    @has_access_api
    def api_conversation_artifacts(self, conversation_id: str) -> FlaskResponse:
        owner = _current_user_id()
        if owner is None:
            return self.json_response(
                {"message": _("No authenticated user"), "severity": "danger"}, 401
            )
        status, body = call_xp_prompt(
            "GET",
            f"/ai/conversations/{conversation_id}/artifacts"
            f"?superset_user_id={owner}",
        )
        return self.json_response(body, status)

    @expose("/api/artifacts/<artifact_id>/<action>/", methods=("POST",))
    @has_access_api
    def api_artifact_action(self, artifact_id: str, action: str) -> FlaskResponse:
        """confirm / discard / publish, all backed by one lifecycle."""
        if action not in _ARTIFACT_ACTIONS:
            return self.json_response(
                {"message": _("Unsupported artifact action"), "severity": "danger"},
                404,
            )
        owner = _current_user_id()
        if owner is None:
            return self.json_response(
                {"message": _("No authenticated user"), "severity": "danger"}, 401
            )
        payload = _body()
        payload["superset_user_id"] = owner
        status, body = call_xp_prompt(
            "POST", f"/ai/artifacts/{artifact_id}/{action}", payload=payload
        )
        return self.json_response(body, status)

    # --- Guided SQL turn: candidate → execute → validate → store ---------

    @expose("/api/conversations/<conversation_id>/sql/", methods=("POST",))
    @has_access_api
    def api_conversation_sql(self, conversation_id: str) -> FlaskResponse:
        """Answer a data question end to end, inside one request.

        The division of labour is deliberate: the service writes and validates the
        SQL and decides what the result may show; this view only authorizes the
        database and runs the statement on Superset's own connection. The finished
        turn is handed back to the service to store, so a transcript can never
        contain a shape this service did not normalize.
        """
        owner = _current_user_id()
        if owner is None:
            return self.json_response(
                {"message": _("No authenticated user"), "severity": "danger"}, 401
            )
        payload = _body()
        question = str(payload.get("question") or "").strip()
        if not question:
            return self.json_response(
                {"message": _("question is required"), "severity": "danger"}, 400
            )
        datasets = authorized_datasets()
        database = _database_for(payload, datasets)
        if database is None:
            return self.json_response(
                {
                    "message": _("Choose a database: the workspace is authorized for several"),
                    "severity": "danger",
                },
                400,
            )

        schema_name = str(payload.get("schema") or "").strip() or None
        candidate_status, candidate = call_xp_prompt(
            "POST",
            "/ai/sql/candidate",
            payload={
                "superset_user_id": owner,
                "question": question,
                "database": database.database_name,
                "max_rows": MAX_PREVIEW_ROWS,
                "datasets": datasets,
            },
        )
        if candidate_status != 200:
            return self.json_response(candidate, candidate_status)

        try:
            security_manager.raise_for_access(
                database=database, sql=candidate["sql"], schema=schema_name
            )
        except SupersetSecurityException:
            return self.json_response(
                {"message": _("Not authorized to run this query"), "severity": "danger"},
                403,
            )

        started = time.monotonic()
        try:
            frame = database.get_df(candidate["sql"], schema=schema_name)
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("XP Prompt SQL turn failed: %s", type(exc).__name__)
            return self.json_response(
                {"message": _("The query could not be executed"), "severity": "danger"},
                400,
            )
        duration_ms = round((time.monotonic() - started) * 1000)

        preview = frame.head(MAX_PREVIEW_ROWS)
        result_status, verdict = call_xp_prompt(
            "POST",
            "/ai/sql/result",
            payload={
                "superset_user_id": owner,
                "columns": [str(column) for column in preview.columns],
                "rows": [
                    list(row) for row in preview.itertuples(index=False, name=None)
                ],
                "row_count": int(len(frame)),
                "truncated": len(frame) > len(preview),
                "intent": "query",
                "max_rows": MAX_PREVIEW_ROWS,
            },
        )
        if result_status != 200:
            return self.json_response(verdict, result_status)

        blocks = [*candidate.get("blocks", []), *verdict.get("blocks", [])]
        store_status, stored = call_xp_prompt(
            "POST",
            f"/ai/conversations/{conversation_id}/blocks",
            payload={
                "superset_user_id": owner,
                "question": question,
                "content": candidate.get("rationale") or "",
                "blocks": blocks,
                "kind": "answer",
                "intent": "query",
                "correlation_id": candidate.get("correlation_id"),
            },
        )
        if store_status not in (200, 201):
            return self.json_response(stored, store_status)

        return self.json_response(
            {
                "message": stored.get("message"),
                "sql": candidate["sql"],
                "duration_ms": duration_ms,
                "limit": candidate.get("limit"),
                "tables": candidate.get("tables", []),
                "database": database.database_name,
            },
            201,
        )

    # --- Guarded execution (P4-F09) --------------------------------------

    @expose("/api/sql/execute/", methods=("POST",))
    @has_access_api
    def api_sql_execute(self) -> FlaskResponse:
        """Execute one guarded statement on Superset's own connection.

        The orchestration service never holds analytics credentials: it produces
        SQL, the guard validates it, and *this* view runs it through the
        ``Database`` the administrator configured (which should be a read-only
        role). Authorization is Superset's — ``raise_for_access`` with the SQL
        text applies the same table/schema checks SQL Lab does.
        """
        owner = _current_user_id()
        if owner is None:
            return self.json_response(
                {"message": _("No authenticated user"), "severity": "danger"}, 401
            )
        payload = _body()
        database_id = payload.get("database_id")
        raw_sql = str(payload.get("sql") or "").strip()
        schema_name = str(payload.get("schema") or "").strip() or None
        max_rows = min(int(payload.get("max_rows") or MAX_PREVIEW_ROWS), MAX_PREVIEW_ROWS)
        if not raw_sql:
            return self.json_response(
                {"message": _("sql is required"), "severity": "danger"}, 400
            )

        database = (
            db.session.query(Database)
            .filter(Database.id == database_id)
            .one_or_none()
            if database_id
            else None
        )
        if database is None:
            return self.json_response(
                {"message": _("Database not found"), "severity": "danger"}, 404
            )

        # Re-validate here: the proxy must not execute what a client claims was
        # validated earlier.
        validate_status, validated = call_xp_prompt(
            "POST",
            "/ai/sql/validate",
            payload={
                "superset_user_id": owner,
                "sql": raw_sql,
                "database": database.database_name,
                "schema": schema_name,
                "max_rows": max_rows,
                "datasets": authorized_datasets(),
            },
        )
        if validate_status != 200:
            return self.json_response(validated, validate_status)

        try:
            security_manager.raise_for_access(
                database=database, sql=validated["sql"], schema=schema_name
            )
        except SupersetSecurityException as exc:
            return self.json_response(
                {
                    "message": _("Not authorized to run this query"),
                    "severity": "danger",
                    "error_type": getattr(exc, "error_type", None),
                },
                403,
            )

        started = time.monotonic()
        try:
            frame = database.get_df(validated["sql"], schema=schema_name)
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("XP Prompt execution failed: %s", type(exc).__name__)
            return self.json_response(
                {"message": _("The query could not be executed"), "severity": "danger"},
                400,
            )
        duration_ms = round((time.monotonic() - started) * 1000)

        preview = frame.head(max_rows)
        columns = [str(column) for column in preview.columns]
        rows = [
            list(row) for row in preview.itertuples(index=False, name=None)
        ]
        # The service owns masking and the preview block (P4-F10): this view must
        # not decide what a caller may see of a result.
        result_status, verdict = call_xp_prompt(
            "POST",
            "/ai/sql/result",
            payload={
                "superset_user_id": owner,
                "columns": columns,
                "rows": rows,
                "row_count": int(len(frame)),
                "truncated": len(frame) > len(preview),
                "intent": payload.get("intent"),
                "max_rows": max_rows,
            },
        )
        if result_status != 200:
            return self.json_response(verdict, result_status)

        return self.json_response(
            {
                **verdict,
                "duration_ms": duration_ms,
                "limit": validated["limit"],
                "tables": validated["tables"],
                "database": database.database_name,
                "schema_fingerprint": validated.get("schema_fingerprint"),
            },
            200,
        )

    # --- Authorized schema slice (P4-F02) -------------------------------
    @expose("/api/schema/", methods=("GET",))
    @has_access_api
    def api_schema_slice(self) -> FlaskResponse:
        """The caller's authorized datasets, resolved by Superset, not guessed.

        The slice is built here because Superset is the authorization authority:
        the service never queries the metadata database for table names, so an
        unauthorized name cannot reach a prompt.
        """
        owner = _current_user_id()
        if owner is None:
            return self.json_response(
                {"message": _("No authenticated user"), "severity": "danger"}, 401
            )
        try:
            payload = {
                "superset_user_id": owner,
                "datasets": authorized_datasets(),
            }
        except Exception:  # pylint: disable=broad-except
            logger.exception("Could not build the authorized schema slice")
            return self.json_response(
                {"message": _("Could not load the schema"), "severity": "danger"},
                503,
            )
        status, body = call_xp_prompt("POST", "/ai/schema", payload=payload)
        return self.json_response(body, status)


#: Browser-facing verb -> XP Prompt endpoint verb. Only these four exist upstream.
_JOB_ACTIONS = {
    "cancel": "cancel",
    "retry": "retry",
    "confirm": "confirm",
    "reject": "reject",
}

#: Artifact transitions. ``publish`` is driven by a producer phase, not the UI.
_ARTIFACT_ACTIONS = ("confirm", "discard", "publish")
#: Bounds on the authorized slice: a prompt is not a schema dump.
MAX_SLICE_DATASETS = 50
MAX_SLICE_COLUMNS = 60

#: Most rows a preview may return. Execution is synchronous, so this is also the
#: practical limit on how much a single request can serialise.
MAX_PREVIEW_ROWS = 200
MAX_CELL_CHARS = 200


def _cell(value: Any) -> str:
    """One result cell as display text; nulls and pandas sentinels become empty."""
    if value is None:
        return ""
    text = str(value)
    if text in {"nan", "NaN", "NaT", "None"}:
        return ""
    return text[:MAX_CELL_CHARS]


def _database_for(payload: dict[str, Any], datasets: list[dict[str, Any]]) -> Any:
    """Resolve the target database from the slice rather than from a client id.

    A caller may name a database explicitly; otherwise the one database the
    principal is authorized for is used, and several candidates are refused so the
    UI must ask rather than guess.
    """
    database_id = payload.get("database_id")
    if database_id not in (None, ""):
        try:
            return db.session.query(Database).filter(Database.id == int(database_id)).one_or_none()
        except (TypeError, ValueError):
            return None
    name = str(payload.get("database") or "").strip()
    if name:
        return db.session.query(Database).filter(Database.database_name == name).one_or_none()
    names = {item["database"] for item in datasets if item.get("database")}
    if len(names) != 1:
        return None
    return db.session.query(Database).filter(Database.database_name == names.pop()).one_or_none()


def authorized_datasets() -> list[dict[str, Any]]:
    """Datasets the current principal may query, straight from the security manager.

    ``get_user_datasources`` already combines explicit dataset permissions with
    implicit database access, so it is the same authority that gates Explore.
    """
    from superset import security_manager

    datasets: list[dict[str, Any]] = []
    for datasource in security_manager.get_user_datasources():
        database = getattr(datasource, "database", None)
        if database is None:
            continue
        columns = [
            {
                "name": column.column_name,
                "type": column.type or "",
                "temporal": bool(column.is_dttm),
            }
            for column in (getattr(datasource, "columns", None) or [])[:MAX_SLICE_COLUMNS]
        ]
        datasets.append(
            {
                "dataset_id": datasource.id,
                "name": datasource.table_name,
                "schema": datasource.schema or "",
                "database": database.database_name,
                "dialect": (database.backend or "").split("://", 1)[0],
                "columns": columns,
            }
        )
    datasets.sort(key=lambda item: (str(item["database"]), str(item["name"])))
    return datasets[:MAX_SLICE_DATASETS]


def _current_user_id() -> int | None:
    """Superset user id of the caller; the only trusted identity source."""
    user = getattr(g, "user", None)
    user_id = getattr(user, "id", None)
    try:
        value = int(user_id)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _body() -> dict[str, Any]:
    payload = request.get_json(silent=True) or {}
    return dict(payload) if isinstance(payload, dict) else {}
