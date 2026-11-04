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
from superset.utils.json import loads as json_loads
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

    @expose("/api/conversations/<conversation_id>/audit/", methods=("GET",))
    @has_access_api
    def api_conversation_audit(self, conversation_id: str) -> FlaskResponse:
        """Every SQL turn of this conversation, including the refused ones (P4-F12).

        The statement itself stays hidden unless the *service* has diagnostics
        enabled and the caller asked for it; this view only forwards the question.
        """
        owner = _current_user_id()
        if owner is None:
            return self.json_response(
                {"message": _("No authenticated user"), "severity": "danger"}, 401
            )
        include_sql = str(request.args.get("include_sql") or "").lower() in {
            "1",
            "true",
            "yes",
        }
        status, body = call_xp_prompt(
            "GET",
            f"/ai/conversations/{conversation_id}/audit"
            f"?superset_user_id={owner}&include_sql={'true' if include_sql else ''}",
        )
        return self.json_response(body, status)

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
        question, database, failure = _turn_target(payload)
        if failure is not None:
            return self._refuse(
                owner, conversation_id, failure, "candidate", "invalid_request"
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
                "datasets": authorized_datasets(),
            },
        )
        if candidate_status != 200:
            return self._refuse(
                owner,
                conversation_id,
                (candidate, candidate_status),
                "candidate",
                str(
                    candidate.get("guard_code")
                    or candidate.get("code")
                    or "candidate"
                ),
            )
        expression = candidate["sql"]
        context = {
            "sql": expression,
            "dialect": str(candidate.get("dialect") or ""),
            "tables": candidate.get("tables"),
            "inlined": candidate.get("inlined"),
            "rls_applied": candidate.get("rls_applied"),
        }

        failure = _authorize(database, expression, schema_name)
        if failure is not None:
            return self._refuse(
                owner, conversation_id, failure, "guard", "not_authorized", context
            )

        # Cost gate (P4-F08): measure here, decide in the service. Refusing before
        # execution is the only useful place to refuse.
        failure, plan = _cost_gate(owner, database, expression, schema_name)
        if failure is not None:
            code = str(failure[0].get("plan_code") or "plan_refused")
            return self._refuse(
                owner,
                conversation_id,
                failure,
                "plan",
                code,
                {**context, "plan": _plan_record(plan)},
            )
        context["plan"] = _plan_record(plan)

        frame, duration_ms, failure = _execute(database, expression, schema_name)
        if failure is not None:
            return self._refuse(
                owner,
                conversation_id,
                failure,
                "execute",
                "execution_failed",
                {**context, "duration_ms": duration_ms},
            )

        # The wall-clock budget is a soft gate: the statement has run, so the
        # honest response is to refuse to present it as a cheap answer rather than
        # to pretend it never happened (a hard timeout belongs to the read-only
        # database role's statement timeout).
        slow = _runtime_budget_failure(duration_ms, plan)
        if slow is not None:
            return self._refuse(
                owner,
                conversation_id,
                slow,
                "execute",
                "runtime_budget_exceeded",
                {**context, "duration_ms": duration_ms},
            )

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
            return self._refuse(
                owner,
                conversation_id,
                (verdict, result_status),
                "result",
                "result_rejected",
            )

        blocks = [
            *candidate.get("blocks", []),
            *verdict.get("blocks", []),
            *_plan_warnings(plan),
        ]
        stored, store_status = _store_turn(
            owner,
            conversation_id,
            question=question,
            content=candidate.get("rationale") or "",
            blocks=blocks,
            correlation_id=candidate.get("correlation_id"),
        )
        if store_status != 201:
            return self._refuse(
                owner, conversation_id, (stored, store_status), "store", "store_failed"
            )

        _audit_turn(
            owner,
            conversation_id,
            outcome="answered",
            stage="execute",
            code="ok",
            correlation_id=candidate.get("correlation_id"),
            duration_ms=duration_ms,
            row_count=verdict.get("row_count"),
            returned=verdict.get("returned"),
            truncated=bool(verdict.get("truncated")),
            **context,
        )

        return self.json_response(
            {
                "message": stored.get("message"),
                "sql": expression,
                "duration_ms": duration_ms,
                "limit": candidate.get("limit"),
                "tables": candidate.get("tables", []),
                "database": database.database_name,
                "plan": plan.get("code"),
            },
            201,
        )

    def _refuse(
        self,
        owner: int,
        conversation_id: str,
        failure: tuple[dict[str, Any], int],
        stage: str,
        code: str,
        context: dict[str, Any] | None = None,
    ) -> FlaskResponse:
        """Answer a refused turn and record why the turn was refused."""
        body, status = failure
        _audit_turn(
            owner,
            conversation_id,
            outcome="refused",
            stage=stage,
            code=code,
            **(context or {}),
        )
        return self.json_response(body, status)

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
        requested = int(payload.get("max_rows") or MAX_PREVIEW_ROWS)
        max_rows = min(requested, MAX_PREVIEW_ROWS)
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
#: A virtual dataset's SQL is forwarded so the service can inline it; the service
#: enforces the same bound, this is only here to keep one request small.
MAX_DATASET_SQL_CHARS = 8_000
#: Rendered row-level predicates travel with the dataset; the service re-bounds and
#: parses them before they reach a statement.
MAX_RLS_CHARS = 2_000
#: A plan probe is a read of the planner only, so its own output is bounded hard:
#: a pathological plan is refused by the service anyway, not summarised here.
MAX_PLAN_ROWS = 200


def _explain_estimate(
    database: Any, sql: str, schema_name: str | None
) -> dict[str, Any]:
    """A normalized cost estimate for a statement, or an honest "unavailable".

    The estimate is measured here because this is the only component with a
    database connection; the *decision* is not made here (P4-F08 owns the budget in
    the service). ``EXPLAIN`` plans a statement without running it, so the probe is
    read-only even for a query that would be expensive.

    An unmeasurable statement is reported as ``unavailable`` rather than guessed:
    the service decides whether that is acceptable for this deployment.
    """
    backend = (getattr(database, "backend", "") or "").split("://", 1)[0]
    try:
        if backend in {"postgresql", "postgres", "postgresql+psycopg2"}:
            frame = database.get_df(f"EXPLAIN (FORMAT JSON) {sql}", schema=schema_name)
            plan = _postgres_plan(frame)
        elif backend in {"mysql", "mariadb", "mysql+pymysql"}:
            frame = database.get_df(f"EXPLAIN {sql}", schema=schema_name)
            plan = _mysql_plan(frame)
        else:
            return {"source": "unavailable"}
    except Exception as exc:  # pylint: disable=broad-except
        logger.info("XP Prompt cost probe unavailable: %s", type(exc).__name__)
        return {"source": "unavailable"}
    if plan is None:
        return {"source": "unavailable"}
    return plan


def _postgres_plan(frame: Any) -> dict[str, Any] | None:
    """PostgreSQL ``EXPLAIN (FORMAT JSON)`` -> rows/cost/nodes.

    ``rows`` is the **largest** row estimate anywhere in the plan, not the root's:
    an aggregate reports one output row while the scan beneath it may read
    millions, and the row budget is about what the query reads.
    """
    if frame is None or frame.empty:
        return None
    raw = frame.iloc[0, 0]
    document = raw
    if isinstance(raw, str):
        try:
            document = json_loads(raw)
        except ValueError:
            return None
    if isinstance(document, list):
        document = document[0] if document else None
    root = (document or {}).get("Plan") if isinstance(document, dict) else None
    if not isinstance(root, dict):
        return None
    nodes, widest = _plan_stats(root)
    cost = _number(root.get("Total Cost"))
    return {
        "source": "explain",
        "rows": widest if widest > 0 else None,
        "cost": float(cost) if cost is not None else None,
        "plan_nodes": nodes,
    }


def _mysql_plan(frame: Any) -> dict[str, Any] | None:
    """MySQL/MariaDB ``EXPLAIN`` -> the product of the per-table row estimates."""
    if frame is None or frame.empty:
        return None
    rows = 1
    seen = False
    for value in frame.get("rows", []):
        estimate = _number(value)
        if estimate is None:
            continue
        rows *= max(1, int(estimate))
        seen = True
    if not seen:
        return None
    return {"source": "explain", "rows": rows, "cost": None, "plan_nodes": len(frame)}


def _plan_stats(plan: dict[str, Any]) -> tuple[int, int]:
    """(node count, widest row estimate) for one plan subtree."""
    nodes = 1
    widest = int(_number(plan.get("Plan Rows")) or 0)
    for child in plan.get("Plans") or []:
        if not isinstance(child, dict):
            continue
        child_nodes, child_widest = _plan_stats(child)
        nodes += child_nodes
        widest = max(widest, child_widest)
    return nodes, widest


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _plan_warnings(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Surface what the cost gate decided, so the transcript is not silent about it."""
    code = str(plan.get("code") or "")
    estimate = plan.get("estimate") or {}
    if code == "plan_unavailable":
        return [{"type": "warnings", "items": [_("No cost estimate was available.")]}]
    rows = estimate.get("rows")
    if code == "ok" and isinstance(rows, int) and rows > 0:
        return [
            {
                "type": "warnings",
                "items": [
                    _("Estimated %(rows)s rows from the query plan.")
                    % {"rows": f"{rows:,}"}
                ],
            }
        ]
    return []


def _plan_record(plan: dict[str, Any]) -> dict[str, Any]:
    """The audit's view of a plan verdict: what was measured and what it decided."""
    estimate = plan.get("estimate") or {}
    return {
        "source": estimate.get("source"),
        "rows": estimate.get("rows"),
        "cost": estimate.get("cost"),
        "nodes": estimate.get("plan_nodes"),
        "code": plan.get("code"),
    }


def _audit_turn(
    owner: int,
    conversation_id: str,
    *,
    outcome: str,
    stage: str,
    code: str = "",
    sql: str | None = None,
    correlation_id: str | None = None,
    plan: dict[str, Any] | None = None,
    duration_ms: int | None = None,
    row_count: int | None = None,
    returned: int | None = None,
    truncated: bool = False,
    **extra: Any,
) -> None:
    """Record one turn in the audit trail (P4-F12).

    Auditing must never break the turn it is recording: a failure here is logged and
    swallowed, because the user's answer is already decided by the time it is called
    and an audit outage must not become a query outage.
    """
    payload = {
        "superset_user_id": owner,
        "conversation_id": conversation_id,
        "outcome": outcome,
        "stage": stage,
        "code": code,
        "correlation_id": correlation_id,
        "intent": "query",
        "dialect": extra.pop("dialect", ""),
        "tables": extra.pop("tables", None),
        "inlined": extra.pop("inlined", None),
        "rls_applied": extra.pop("rls_applied", None),
        "plan": plan,
        "duration_ms": duration_ms,
        "row_count": row_count,
        "returned": returned,
        "truncated": truncated,
        "sql": sql,
    }
    try:
        status, body = call_xp_prompt("POST", "/ai/sql/audit", payload=payload)
        if status not in (200, 201):
            logger.warning(
                "XP Prompt audit not stored: %s (%s)",
                status,
                body.get("message") if isinstance(body, dict) else "",
            )
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("XP Prompt audit failed: %s", type(exc).__name__)


def _turn_target(
    payload: dict[str, Any],
) -> tuple[str, Any, tuple[dict[str, Any], int] | None]:
    """What to ask, against which database — or why the request cannot proceed.

    The database is resolved from the principal's authorized slice, never from an
    id the browser picked, so a stale or hostile id cannot redirect the query.
    """
    question = str(payload.get("question") or "").strip()
    if not question:
        return "", None, (
            {"message": _("question is required"), "severity": "danger"},
            400,
        )
    database = _database_for(payload, authorized_datasets())
    if database is None:
        return question, None, (
            {
                "message": _(
                    "Choose a database: the workspace is authorized for several"
                ),
                "severity": "danger",
            },
            400,
        )
    return question, database, None


def _authorize(
    database: Any, sql: str, schema_name: str | None
) -> tuple[dict[str, Any], int] | None:
    """Superset's own access check, including the tables the SQL references."""
    try:
        security_manager.raise_for_access(
            database=database, sql=sql, schema=schema_name
        )
    except SupersetSecurityException:
        return (
            {"message": _("Not authorized to run this query"), "severity": "danger"},
            403,
        )
    return None


def _execute(
    database: Any, sql: str, schema_name: str | None
) -> tuple[Any, int, tuple[dict[str, Any], int] | None]:
    """Run the statement on Superset's own connection and time it.

    Returns ``(frame, duration_ms, failure)``; on failure ``frame`` is ``None`` and
    ``failure`` carries the body and status to send. The exception *type* is logged
    and the message is not, so a driver error cannot echo user data.
    """
    started = time.monotonic()
    try:
        frame = database.get_df(sql, schema=schema_name)
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("XP Prompt execution failed: %s", type(exc).__name__)
        return (
            None,
            round((time.monotonic() - started) * 1000),
            (
                {"message": _("The query could not be executed"), "severity": "danger"},
                400,
            ),
        )
    return frame, round((time.monotonic() - started) * 1000), None


def _store_turn(
    owner: int,
    conversation_id: str,
    *,
    question: str,
    content: str,
    blocks: list[dict[str, Any]],
    correlation_id: str | None,
) -> tuple[dict[str, Any], int]:
    """Hand the finished turn back to the service, which owns the block contract."""
    status, body = call_xp_prompt(
        "POST",
        f"/ai/conversations/{conversation_id}/blocks",
        payload={
            "superset_user_id": owner,
            "question": question,
            "content": content,
            "blocks": blocks,
            "kind": "answer",
            "intent": "query",
            "correlation_id": correlation_id,
        },
    )
    if status in (200, 201):
        return body, 201
    return body, status


def _cost_gate(
    owner: int, database: Any, sql: str, schema_name: str | None
) -> tuple[tuple[dict[str, Any], int] | None, dict[str, Any]]:
    """Measure the statement here, ask the service whether it may run.

    Returns ``(failure, plan)``: ``failure`` is ``None`` when the statement is
    within budget, otherwise the response body and status to send. The service owns
    the budget so a caller cannot talk its own query into being cheap.
    """
    estimate = _explain_estimate(database, sql, schema_name)
    status, plan = call_xp_prompt(
        "POST",
        "/ai/sql/plan",
        payload={
            "superset_user_id": owner,
            "estimate": estimate,
            "max_rows": MAX_PREVIEW_ROWS,
        },
    )
    if status != 200:
        return (plan, status), {}
    if not plan.get("allowed"):
        return (
            {
                "message": plan.get("message") or _("The query is too expensive"),
                "severity": "warning",
                "plan_code": plan.get("code"),
            },
            422,
        ), plan
    return None, plan


def _runtime_budget_failure(
    duration_ms: int, plan: dict[str, Any]
) -> tuple[dict[str, Any], int] | None:
    """Refuse to present a slow answer as a cheap one (soft wall-clock budget)."""
    budget = plan.get("budget") or {}
    try:
        max_runtime_ms = int(budget.get("max_runtime_ms") or 0)
    except (TypeError, ValueError):
        return None
    if not max_runtime_ms or duration_ms <= max_runtime_ms:
        return None
    return (
        {
            "message": _(
                "This query took %(ms)s ms, above the %(budget)s ms budget. Narrow "
                "the question and try again."
            )
            % {"ms": duration_ms, "budget": max_runtime_ms},
            "severity": "warning",
            "plan_code": "runtime_budget_exceeded",
        },
        422,
    )


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
            return (
                db.session.query(Database)
                .filter(Database.id == int(database_id))
                .one_or_none()
            )
        except (TypeError, ValueError):
            return None
    name = str(payload.get("database") or "").strip()
    if name:
        return (
            db.session.query(Database)
            .filter(Database.database_name == name)
            .one_or_none()
        )
    names = {item["database"] for item in datasets if item.get("database")}
    if len(names) != 1:
        return None
    return (
        db.session.query(Database)
        .filter(Database.database_name == names.pop())
        .one_or_none()
    )


def authorized_datasets() -> list[dict[str, Any]]:
    """Datasets the current principal may query, straight from the security manager.

    ``get_user_datasources`` already combines explicit dataset permissions with
    implicit database access, so it is the same authority that gates Explore.

    A *virtual* dataset carries its own SQL: the name is a governed query, not a
    physical table, so the pipeline needs the definition to reference it at all.
    It is included as-is (bounded) because it is admin-authored, and the service
    validates it before inlining; a templated definition is dropped there.

    Row-level security is resolved here, by Superset, because only Superset knows
    the principal's roles and how to render the rule (``{{ current_username() }}``
    and friends). The rendered clauses travel with the dataset and the service
    applies them to every reference; a dataset whose rules cannot be rendered is
    left out of the slice entirely, so a policy failure can only remove data from
    the answer, never widen it.
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
            for column in (
                getattr(datasource, "columns", None) or []
            )[:MAX_SLICE_COLUMNS]
        ]
        definition = getattr(datasource, "sql", None) or ""
        try:
            rls_filters = [
                str(rule) for rule in datasource.get_sqla_row_level_filters()
            ]
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning(
                "Skipping dataset %s: its row-level rules could not be rendered (%s)",
                getattr(datasource, "id", "?"),
                type(exc).__name__,
            )
            continue
        datasets.append(
            {
                "dataset_id": datasource.id,
                "name": datasource.table_name,
                "schema": datasource.schema or "",
                "database": database.database_name,
                "dialect": (database.backend or "").split("://", 1)[0],
                "sql": str(definition)[:MAX_DATASET_SQL_CHARS],
                "rls": [clause[:MAX_RLS_CHARS] for clause in rls_filters],
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
