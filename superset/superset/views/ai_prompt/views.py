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

import hashlib
import logging
import os
import threading
import time
from contextlib import contextmanager
from collections.abc import Iterator
from typing import Any
from urllib.parse import quote

from flask import g, request
from flask_appbuilder import expose
from flask_appbuilder.security.decorators import has_access, has_access_api
from flask_babel import gettext as _

from superset import db, security_manager
from superset.daos.dashboard import DashboardDAO
from superset.exceptions import SupersetSecurityException
from superset.models.core import Database
from superset.superset_typing import FlaskResponse
from superset.utils.json import dumps as json_dumps, loads as json_loads
from superset.views.ai_prompt import dashboard_publish
from superset.views.ai_prompt.dashboard_publish import (
    MODE_CREATE,
    MODE_DUPLICATE,
    MODE_UPDATE,
)
from superset.views.ai_provider.views import call_xp_prompt
from superset.views.base import BaseSupersetView

logger = logging.getLogger(__name__)

#: ``sql.max_concurrent`` when the service cannot report it (an older service, or
#: a failed settings read). One, not four: an unknown limit must not be generous.
DEFAULT_MAX_CONCURRENT = 1

#: Statements this worker process is running, and the lock that counts them.
#: The limit is published by the service, but the counter is process-local: a
#: shared one would need Redis, so what this bounds is the work one worker can
#: start — which is where the connections and the CPU actually go.
_ACTIVE_SQL = 0
_ACTIVE_SQL_LOCK = threading.Lock()


@contextmanager
def _concurrency_slot(max_concurrent: int) -> Iterator[bool]:
    """Take one execution slot, or report that none is free.

    Refusing before the query starts is the point: queueing it would only move
    the pressure, and the caller can retry once the answer is worth waiting for.
    """
    global _ACTIVE_SQL
    limit = max(1, int(max_concurrent))
    with _ACTIVE_SQL_LOCK:
        if _ACTIVE_SQL >= limit:
            yield False
            return
        _ACTIVE_SQL += 1
    try:
        yield True
    finally:
        with _ACTIVE_SQL_LOCK:
            _ACTIVE_SQL -= 1


def _safeguards(validated: dict[str, Any]) -> dict[str, Any]:
    """The service's safeguard set, with a defensive fallback for each value."""
    published = validated.get("limits")
    limits = published if isinstance(published, dict) else {}
    return {
        "max_rows": limits.get("max_rows"),
        "max_concurrent": limits.get("max_concurrent") or DEFAULT_MAX_CONCURRENT,
        "timeout_ms": limits.get("timeout_ms"),
    }


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

    # --- Operator view of the repair knowledge (P5-F10) ------------------

    @expose("/api/repair/metrics/", methods=("GET",))
    @has_access_api
    def api_repair_metrics(self) -> FlaskResponse:
        """Counts and promotion settings — the numbers, never a statement."""
        owner = _current_user_id()
        if owner is None:
            return self.json_response(
                {"message": _("No authenticated user"), "severity": "danger"}, 401
            )
        status, body = call_xp_prompt(
            "GET", f"/ai/sql/repair/metrics?superset_user_id={owner}"
        )
        return self.json_response(body, status)

    @expose("/api/repair/patterns/", methods=("GET",))
    @has_access_api
    def api_repair_patterns(self) -> FlaskResponse:
        """The stored corrections, with their standing and why they rank as they do."""
        owner = _current_user_id()
        if owner is None:
            return self.json_response(
                {"message": _("No authenticated user"), "severity": "danger"}, 401
            )
        query = f"/ai/sql/patterns?superset_user_id={owner}"
        state = str(request.args.get("state") or "").strip().lower()
        if state:
            query = f"{query}&state={state}"
        status, body = call_xp_prompt("GET", query)
        return self.json_response(body, status)

    @expose("/api/repair/patterns/<pattern_id>/<action>/", methods=("POST",))
    @has_access_api
    def api_repair_pattern_action(self, pattern_id: str, action: str) -> FlaskResponse:
        """promote / deprecate / quarantine / restore — an operator's brake (P5-F09)."""
        if action not in _PATTERN_ACTIONS:
            return self.json_response(
                {"message": _("Unsupported pattern action"), "severity": "danger"}, 404
            )
        owner = _current_user_id()
        if owner is None:
            return self.json_response(
                {"message": _("No authenticated user"), "severity": "danger"}, 401
            )
        payload = _body()
        payload["superset_user_id"] = owner
        status, body = call_xp_prompt(
            "POST", f"/ai/sql/patterns/{pattern_id}/{action}", payload=payload
        )
        return self.json_response(body, status)

    # --- Chart recommendation, preview and save (P7-F06/F07/F08/F10) -----

    @expose("/api/charts/recommend/", methods=("POST",))
    @has_access_api
    def api_charts_recommend(self) -> FlaskResponse:
        """Recommend visualizations for a dataset the caller may already use.

        The proxy supplies what this deployment can render and what the columns look
        like; the service owns the rules and the control contracts. Nothing here decides
        what is appropriate — it only measures and authorizes.
        """
        owner = _current_user_id()
        if owner is None:
            return self.json_response(
                {"message": _("No authenticated user"), "severity": "danger"}, 401
            )
        payload = _body()
        dataset, failure = _chart_dataset(payload.get("dataset_id"))
        if failure is not None:
            return self.json_response(*failure)
        measured = _chart_measured(dataset) if payload.get("measure", True) else {}
        status, body = call_xp_prompt(
            "POST",
            "/ai/charts/recommend",
            payload={
                "superset_user_id": owner,
                **_chart_catalog_payload(),
                "columns": _chart_columns(dataset),
                "measured": measured.get("columns"),
                "row_count": measured.get("row_count"),
                "question": str(payload.get("question") or ""),
                "rank_with_model": bool(payload.get("rank_with_model")),
                "include_profile": True,
            },
        )
        if status != 200:
            return self.json_response(body, status)
        body["dataset"] = _chart_dataset_summary(dataset)
        body["measured"] = bool(measured)
        return self.json_response(body, 200)

    @expose("/api/charts/drafts/", methods=("POST",))
    @has_access_api
    def api_chart_draft_create(self) -> FlaskResponse:
        """Turn one chosen candidate into a validated draft (P7-F05)."""
        owner = _current_user_id()
        if owner is None:
            return self.json_response(
                {"message": _("No authenticated user"), "severity": "danger"}, 401
            )
        payload = _body()
        dataset, failure = _chart_dataset(payload.get("dataset_id"))
        if failure is not None:
            return self.json_response(*failure)
        candidate = (
            payload.get("candidate")
            if isinstance(payload.get("candidate"), dict)
            else {}
        )
        status, body = call_xp_prompt(
            "POST",
            "/ai/charts/drafts",
            payload={
                "superset_user_id": owner,
                **_chart_catalog_payload(),
                "viz_type": str(candidate.get("viz_type") or ""),
                "title": str(payload.get("title") or candidate.get("title") or ""),
                "dataset_id": int(dataset.id),
                "dataset_name": str(dataset.table_name or ""),
                "database": str(getattr(dataset.database, "database_name", "") or ""),
                "form_data": candidate.get("form_data") or {},
                "rationale": str(candidate.get("rationale") or ""),
                "candidate_id": str(candidate.get("id") or ""),
                "ranking_source": str(payload.get("ranking_source") or ""),
                "conversation_id": str(payload.get("conversation_id") or ""),
                "correlation_id": str(payload.get("correlation_id") or ""),
                "idempotency_key": str(payload.get("idempotency_key") or ""),
            },
        )
        return self.json_response(body, status)

    @expose("/api/charts/preview/", methods=("POST",))
    @has_access_api
    def api_chart_preview(self) -> FlaskResponse:
        """Render a draft's data through Superset, in the caller's own context (P7-F06).

        This is the real query path — ``QueryContextFactory`` plus ``ChartDataCommand``
        — not a mock: the numbers a user sees are the numbers their permissions allow.
        The preview blocks (masking, row caps) are still the service's decision.
        """
        owner = _current_user_id()
        if owner is None:
            return self.json_response(
                {"message": _("No authenticated user"), "severity": "danger"}, 401
            )
        payload = _body()
        draft_id = str(payload.get("draft_id") or "").strip()
        if not draft_id:
            return self.json_response(
                {"message": _("draft_id is required"), "severity": "danger"}, 400
            )
        status, body = call_xp_prompt(
            "GET", f"/ai/charts/drafts/{draft_id}?superset_user_id={owner}"
        )
        if status != 200:
            return self.json_response(body, status)
        draft = body.get("draft") or {}
        dataset, failure = _chart_dataset(draft.get("dataset_id"))
        if failure is not None:
            return self.json_response(*failure)

        frame, failure = _chart_frame(dataset, draft)
        if failure is not None:
            hint, kind = _classify_failure(owner, "", failure)
            return self.json_response(
                {
                    "message": hint,
                    "severity": "danger",
                    "code": "chart_render_failed",
                    "failure": kind,
                },
                422,
            )
        preview = frame.head(MAX_PREVIEW_ROWS)
        result_status, verdict = call_xp_prompt(
            "POST",
            "/ai/sql/result",
            payload={
                "superset_user_id": owner,
                "columns": [str(column) for column in preview.columns],
                "rows": [
                    [_cell(value) for value in row]
                    for row in preview.itertuples(index=False, name=None)
                ],
                "row_count": int(len(frame)),
                "truncated": len(frame) > len(preview),
                "intent": "query",
                "max_rows": MAX_PREVIEW_ROWS,
            },
        )
        if result_status != 200:
            return self.json_response(verdict, result_status)
        call_xp_prompt(
            "POST",
            f"/ai/charts/drafts/{draft_id}/runs",
            payload={"superset_user_id": owner},
        )
        return self.json_response(
            {
                **verdict,
                "viz_type": draft.get("viz_type"),
                "dataset_id": draft.get("dataset_id"),
            },
            200,
        )

    @expose("/api/charts/drafts/<draft_id>/save/", methods=("POST",))
    @has_access_api
    def api_chart_draft_save(self, draft_id: str) -> FlaskResponse:
        """Create the chart through Superset's command, then read it back (F08/F10)."""
        owner = _current_user_id()
        if owner is None:
            return self.json_response(
                {"message": _("No authenticated user"), "severity": "danger"}, 401
            )
        status, body = call_xp_prompt(
            "GET", f"/ai/charts/drafts/{draft_id}?superset_user_id={owner}"
        )
        if status != 200:
            return self.json_response(body, status)
        draft = body.get("draft") or {}
        if draft.get("status") != "draft":
            return self.json_response(
                {
                    "message": _("This draft is %(status)s and cannot be saved again")
                    % {"status": draft.get("status")},
                    "severity": "warning",
                    "code": "not_open",
                },
                409,
            )
        dataset, failure = _chart_dataset(draft.get("dataset_id"))
        if failure is not None:
            return self.json_response(*failure)
        try:
            chart = _create_chart(owner, dataset, draft)
        except Exception as exc:  # pylint: disable=broad-except
            hint, kind = _classify_failure(owner, "", f"{type(exc).__name__}: {exc}")
            _scrap_chart_draft(owner, draft_id, f"save_failed:{kind}")
            logger.warning("XP Prompt chart save failed: %s", type(exc).__name__)
            return self.json_response(
                {
                    "message": hint,
                    "severity": "danger",
                    "code": "chart_save_failed",
                    "failure": kind,
                },
                422,
            )
        call_xp_prompt(
            "POST",
            f"/ai/charts/drafts/{draft_id}/saved",
            payload={"superset_user_id": owner, "slice_id": int(chart.id)},
        )
        return self.json_response(
            {"chart": _chart_record(chart), "blocks": _chart_saved_blocks(chart)},
            201,
        )

    @expose("/api/charts/drafts/<draft_id>/scrap/", methods=["POST"])
    @has_access_api
    def api_chart_draft_scrap(self, draft_id: str) -> FlaskResponse:
        owner = _current_user_id()
        if owner is None:
            return self.json_response(
                {"message": _("No authenticated user"), "severity": "danger"}, 401
            )
        payload = _body()
        status, body = call_xp_prompt(
            "POST",
            f"/ai/charts/drafts/{draft_id}/scrap",
            payload={
                "superset_user_id": owner,
                "reason": str(payload.get("reason") or "discarded"),
            },
        )
        return self.json_response(body, status)

    # --- Dashboard plan and layout (P8-F01..F05) -------------------------

    # --- Usage ledger, budgets and billing (P9-F08) ----------------------
    #
    # The browser never holds the service token, so every usage read and write
    # goes through here. These routes are deliberately thin: the service owns the
    # numbers, and a proxy that recomputed them would eventually disagree with the
    # ledger it is reporting on.

    # --- Administrative configuration (P10) ------------------------------

    @expose("/api/settings/", methods=("GET",))
    @has_access_api
    def api_settings(self) -> FlaskResponse:
        """The typed registry with each setting's effective value and source."""
        status, body = call_xp_prompt("GET", f"/ai/settings?{_usage_query()}")
        return self.json_response(body, status)

    @expose("/api/settings/", methods=("PUT",))
    @has_access_api
    def api_settings_update(self) -> FlaskResponse:
        """Validate and write a batch; the service owns bounds and auditing."""
        payload = _body()
        # The actor is the signed-in principal, never a claim from the browser:
        # an audit trail that names whoever the client says it is is not an audit
        # trail.
        payload["actor"] = _actor_label()
        status, body = call_xp_prompt("PUT", "/ai/settings", payload=payload)
        return self.json_response(body, status)

    @expose("/api/settings/history/", methods=("GET",))
    @has_access_api
    def api_settings_history(self) -> FlaskResponse:
        status, body = call_xp_prompt(
            "GET", f"/ai/settings/history?{_usage_query()}"
        )
        return self.json_response(body, status)

    @expose("/api/settings/rollback/<audit_id>/", methods=("POST",))
    @has_access_api
    def api_settings_rollback(self, audit_id: str) -> FlaskResponse:
        payload = _body()
        payload["actor"] = _actor_label()
        status, body = call_xp_prompt(
            "POST", f"/ai/settings/rollback/{audit_id}", payload=payload
        )
        return self.json_response(body, status)

    @expose("/api/usage/summary/", methods=("GET",))
    @has_access_api
    def api_usage_summary(self) -> FlaskResponse:
        status, body = call_xp_prompt(
            "GET", f"/ai/usage/summary?{_usage_query()}"
        )
        return self.json_response(body, status)

    @expose("/api/usage/attempts/", methods=("GET",))
    @has_access_api
    def api_usage_attempts(self) -> FlaskResponse:
        status, body = call_xp_prompt(
            "GET", f"/ai/usage/attempts?{_usage_query()}"
        )
        return self.json_response(body, status)

    @expose("/api/usage/reconciliation/", methods=("GET",))
    @has_access_api
    def api_usage_reconciliation(self) -> FlaskResponse:
        status, body = call_xp_prompt(
            "GET", f"/ai/usage/reconciliation?{_usage_query()}"
        )
        return self.json_response(body, status)

    @expose("/api/usage/prices/", methods=("GET", "POST"))
    @has_access_api
    def api_usage_prices(self) -> FlaskResponse:
        if request.method == "POST":
            status, body = call_xp_prompt(
                "POST", "/ai/usage/prices", payload=_body()
            )
            return self.json_response(body, status)
        status, body = call_xp_prompt("GET", f"/ai/usage/prices?{_usage_query()}")
        return self.json_response(body, status)

    @expose("/api/usage/budgets/", methods=("GET", "POST"))
    @has_access_api
    def api_usage_budgets(self) -> FlaskResponse:
        if request.method == "POST":
            status, body = call_xp_prompt(
                "POST", "/ai/usage/budgets", payload=_body()
            )
            return self.json_response(body, status)
        status, body = call_xp_prompt("GET", f"/ai/usage/budgets?{_usage_query()}")
        return self.json_response(body, status)

    @expose("/api/usage/budgets/<budget_id>/", methods=("DELETE",))
    @has_access_api
    def api_usage_budget_delete(self, budget_id: str) -> FlaskResponse:
        status, body = call_xp_prompt(
            "DELETE", f"/ai/usage/budgets/{budget_id}?{_usage_query()}"
        )
        return self.json_response(body, status)

    @expose("/api/usage/outbox/", methods=("GET",))
    @has_access_api
    def api_usage_outbox(self) -> FlaskResponse:
        status, body = call_xp_prompt("GET", f"/ai/usage/outbox?{_usage_query()}")
        return self.json_response(body, status)

    @expose("/api/usage/outbox/drain/", methods=("POST",))
    @has_access_api
    def api_usage_outbox_drain(self) -> FlaskResponse:
        status, body = call_xp_prompt(
            "POST", "/ai/usage/outbox/drain", payload=_body()
        )
        return self.json_response(body, status)

    @expose("/api/usage/billing/imports/", methods=("GET", "POST"))
    @has_access_api
    def api_usage_billing_imports(self) -> FlaskResponse:
        if request.method == "POST":
            status, body = call_xp_prompt(
                "POST", "/ai/usage/billing/imports", payload=_body()
            )
            return self.json_response(body, status)
        status, body = call_xp_prompt(
            "GET", f"/ai/usage/billing/imports?{_usage_query()}"
        )
        return self.json_response(body, status)

    @expose("/api/usage/billing/imports/<import_id>/variance/", methods=("GET",))
    @has_access_api
    def api_usage_billing_variance(self, import_id: str) -> FlaskResponse:
        status, body = call_xp_prompt(
            "GET",
            f"/ai/usage/billing/imports/{import_id}/variance?{_usage_query()}",
        )
        return self.json_response(body, status)

    @expose("/api/usage/billing/source/", methods=("GET",))
    @has_access_api
    def api_usage_billing_source(self) -> FlaskResponse:
        """Whether an export source is configured. The URL and token stay secret."""
        status, body = call_xp_prompt("GET", "/ai/usage/billing/source")
        return self.json_response(body, status)

    @expose("/api/usage/billing/fetch/", methods=("POST",))
    @has_access_api
    def api_usage_billing_fetch(self) -> FlaskResponse:
        """Fetch the provider's export now, instead of waiting for the nightly job."""
        status, body = call_xp_prompt(
            "POST", "/ai/usage/billing/fetch", payload=_body()
        )
        return self.json_response(body, status)

    @expose("/api/dashboards/plan/", methods=("POST",))
    @has_access_api
    def api_dashboard_plan(self) -> FlaskResponse:
        """Plan a dashboard from a goal, using only datasets the caller may open.

        The proxy contributes the deployment's facts — the authorized datasets with
        their real columns, the charts already saved on them, and the viz types this
        build can draw — and the service owns the planning rules. Nothing is created
        here: the plan lists what must still be generated before anything could be
        published.
        """
        owner = _current_user_id()
        if owner is None:
            return self.json_response(
                {"message": _("No authenticated user"), "severity": "danger"}, 401
            )
        payload = _body()
        datasets, failure = _plan_datasets(payload.get("dataset_ids"))
        if failure is not None:
            return self.json_response(*failure)
        status, body = call_xp_prompt(
            "POST",
            "/ai/dashboards/plan",
            payload={
                "superset_user_id": owner,
                **_chart_catalog_payload(),
                "goal": str(payload.get("goal") or "").strip(),
                "datasets": [_plan_dataset(dataset) for dataset in datasets],
                "charts": _plan_charts(datasets),
                "candidates": payload.get("candidates") or {},
                "time_grain": payload.get("time_grain"),
            },
        )
        if status != 200:
            return self.json_response(body, status)
        body["columns"] = _plan_column_kinds(datasets)
        return self.json_response(body, 200)

    @expose("/api/dashboards/layout/", methods=("POST",))
    @has_access_api
    def api_dashboard_layout(self) -> FlaskResponse:
        """Validate a plan's layout and native filters before anything is persisted.

        A slot whose chart does not exist, a row wider than the grid, or a filter on a
        column two in-scope charts do not share is reported here — a blank dashboard in
        the browser is not a useful error message.
        """
        owner = _current_user_id()
        if owner is None:
            return self.json_response(
                {"message": _("No authenticated user"), "severity": "danger"}, 401
            )
        payload = _body()
        status, body = call_xp_prompt(
            "POST",
            "/ai/dashboards/layout",
            payload={
                "superset_user_id": owner,
                "title": str(payload.get("title") or ""),
                "slots": payload.get("slots") or [],
                "columns": payload.get("columns") or {},
                "max_filters": payload.get("max_filters"),
            },
        )
        return self.json_response(body, status)

    @expose("/api/dashboards/preview/", methods=("POST",))
    @has_access_api
    def api_dashboard_preview(self) -> FlaskResponse:
        """Render every planned slot with real data before anything is persisted.

        All slots are resolved and authorized first, so an unauthorized later slot
        cannot cause earlier slot data to be returned. Saved charts use their stored
        configuration; generated slots use the already contract-checked form data from
        the plan. Every query then runs through ``ChartDataCommand`` in this caller's
        security context, including RLS.
        """
        owner = _current_user_id()
        if owner is None:
            return self.json_response(
                {"message": _("No authenticated user"), "severity": "danger"}, 401
            )
        payload = _body()
        resolved, failure = _dashboard_preview_slots(payload.get("slots"))
        if failure is not None:
            return self.json_response(*failure)

        renders: list[dict[str, Any]] = []
        for entry in resolved:
            frame, error = _chart_frame(entry["dataset"], entry["draft"])
            if error is not None:
                hint, kind = _classify_failure(owner, "", error)
                return self.json_response(
                    {
                        "message": hint,
                        "severity": "danger",
                        "code": "dashboard_preview_failed",
                        "failure": kind,
                        "slot_id": entry["slot"]["id"],
                    },
                    422,
                )
            preview = frame.head(MAX_PREVIEW_ROWS)
            renders.append(
                {
                    "slot_id": entry["slot"]["id"],
                    "columns": [str(column) for column in preview.columns],
                    "rows": [
                        [_cell(value) for value in row]
                        for row in preview.itertuples(index=False, name=None)
                    ],
                    "row_count": int(len(frame)),
                    "truncated": len(frame) > len(preview),
                }
            )

        status, body = call_xp_prompt(
            "POST",
            "/ai/dashboards/preview",
            payload={
                "superset_user_id": owner,
                "title": str(payload.get("title") or ""),
                "goal": str(payload.get("goal") or ""),
                "audience": str(payload.get("audience") or "unspecified"),
                "slots": [entry["slot"] for entry in resolved],
                "renders": renders,
            },
        )
        return self.json_response(body, status)

    @expose("/api/dashboards/publish/", methods=("POST",))
    @has_access_api
    def api_dashboard_publish(self) -> FlaskResponse:
        """Publish a reviewed dashboard, tracking every object it creates.

        The order is deliberate. The attempt is opened *first*, so a refused access
        assignment or a stale update fails before a single chart exists. Generated
        slots are then created as charts, the layout is built from their real ids, and
        only then does the dashboard command run. Any failure unwinds in reverse and
        is recorded with one outcome per object (P8-F08..F11).
        """
        owner = _current_user_id()
        if owner is None:
            return self.json_response(
                {"message": _("No authenticated user"), "severity": "danger"}, 401
            )

        payload = _body()
        mode = str(payload.get("mode") or MODE_CREATE).strip().lower()
        if mode not in _PUBLISH_MODES:
            return self.json_response(
                {
                    "message": _("Publishing needs an explicit mode"),
                    "severity": "danger",
                    "code": "invalid_mode",
                },
                400,
            )

        resolved, failure = _dashboard_preview_slots(payload.get("slots"))
        if failure is not None:
            return self.json_response(*failure)

        target: Any = None
        if mode in _PUBLISH_MODES_WITH_TARGET:
            target = DashboardDAO.find_by_id(payload.get("dashboard_id"))
            if target is None:
                return self.json_response(
                    {
                        "message": _("Dashboard not found"),
                        "severity": "danger",
                        "code": "dashboard_not_found",
                    },
                    404,
                )

        status, body = call_xp_prompt(
            "POST",
            "/ai/dashboards/publish",
            payload=_publish_intent(owner, mode, payload, resolved, target),
        )
        if status not in (200, 201):
            return self.json_response(body, status)

        replay = _publish_replay(body)
        if replay is not None:
            return self.json_response(*replay)

        publish_id = str(body.get("id") or "")
        publisher = dashboard_publish.DashboardPublisher(owner)
        try:
            metadata, position = _publish_objects(
                owner, mode, publisher, resolved, target, payload, publish_id, body
            )
        except dashboard_publish.PublishBlockedError as exc:
            return self._publish_failed(
                owner, publish_id, publisher, exc.code, str(exc)
            )
        except Exception as exc:  # pylint: disable=broad-except
            hint, kind = _classify_failure(owner, "", f"{type(exc).__name__}: {exc}")
            return self._publish_failed(
                owner, publish_id, publisher, f"publish_failed:{kind}", hint
            )

        return self._publish_done(owner, publish_id, publisher, metadata, position)

    def _publish_done(
        self,
        owner: int,
        publish_id: str,
        publisher: Any,
        metadata: dict[str, Any],
        position: Any,
    ) -> FlaskResponse:
        """Close the attempt: the dashboard exists, so record what made it."""
        status, record = call_xp_prompt(
            "POST",
            f"/ai/dashboards/publish/{publish_id}/published",
            payload={
                "superset_user_id": owner,
                "dashboard_id": publisher.dashboard_id,
                "dashboard_slug": str(getattr(publisher.dashboard, "slug", "") or ""),
                "position": position,
                "created": publisher.created_objects(),
            },
        )
        if status != 200:
            return self.json_response(record, status)
        return self.json_response(
            {
                "publish": record,
                "dashboard_id": publisher.dashboard_id,
                "metadata": metadata,
                "created": publisher.created_objects(),
                "replayed": False,
                "blocks": _published_dashboard_blocks(record),
            },
            201,
        )

    def _publish_failed(
        self,
        owner: int,
        publish_id: str,
        publisher: Any,
        code: str,
        message: str,
    ) -> FlaskResponse:
        """Compensate in reverse, then record what could not be undone."""
        cleanup = publisher.compensate()
        call_xp_prompt(
            "POST",
            f"/ai/dashboards/publish/{publish_id}/failed",
            payload={
                "superset_user_id": owner,
                "error_code": code,
                "error_message": message,
                "created": publisher.created_objects(),
                "cleanup": cleanup,
            },
        )
        left_behind = [
            entry for entry in cleanup if entry["outcome"].startswith("left_behind")
        ]
        return self.json_response(
            {
                "message": message,
                "severity": "danger",
                "code": code,
                "publish_id": publish_id,
                "cleanup": cleanup,
                "left_behind": left_behind,
            },
            422,
        )

    # --- Dataset drafts and save (P6-F01/F03/F06/F07/F09/F10) ------------

    @expose("/api/datasets/equivalents/", methods=("POST",))
    @has_access_api
    def api_dataset_equivalents(self) -> FlaskResponse:
        """Does an equivalent dataset the caller may use already exist? (P6-F01)

        Answered from the *authorized* slice, so the search itself cannot reveal a
        dataset the caller could not open. Reuse is the default answer: creating a
        second copy of the same query is the expensive mistake.
        """
        owner = _current_user_id()
        if owner is None:
            return self.json_response(
                {"message": _("No authenticated user"), "severity": "danger"}, 401
            )
        payload = _body()
        database_name = str(payload.get("database") or "").strip()
        if not database_name:
            return self.json_response(
                {"message": _("database is required"), "severity": "danger"}, 400
            )
        matches = _dataset_equivalents(
            database_name,
            sql=str(payload.get("sql") or ""),
            table_name=str(payload.get("table_name") or ""),
        )
        return self.json_response(
            {"count": len(matches), "result": matches}, 200
        )

    @expose("/api/datasets/drafts/", methods=("GET",))
    @has_access_api
    def api_dataset_drafts(self) -> FlaskResponse:
        owner = _current_user_id()
        if owner is None:
            return self.json_response(
                {"message": _("No authenticated user"), "severity": "danger"}, 401
            )
        status, body = call_xp_prompt(
            "GET", f"/ai/datasets/drafts?superset_user_id={owner}"
        )
        return self.json_response(body, status)

    @expose("/api/datasets/drafts/", methods=("POST",))
    @has_access_api
    def api_dataset_draft_create(self) -> FlaskResponse:
        """Preview, judge, and store a draft — without creating anything (P6-F03/F05).

        The preview runs through the same guarded execution as any SQL turn
        (re-validated, authorized, cost-checked, capped), because a dataset built on a
        definition that was never safe to run would be safe for nobody. The service
        then compiles the spec and applies the quality rules.
        """
        owner = _current_user_id()
        if owner is None:
            return self.json_response(
                {"message": _("No authenticated user"), "severity": "danger"}, 401
            )
        payload = _body()
        spec = payload.get("spec") if isinstance(payload.get("spec"), dict) else {}
        database, failure = _dataset_database(spec)
        if failure is not None:
            return self.json_response(*failure)
        schema_name = str(spec.get("schema") or "").strip() or None
        sql = str(spec.get("sql") or "").strip()

        profile: dict[str, Any] = {}
        if sql:
            profile, failure = _dataset_preview(
                owner,
                database,
                sql=sql,
                schema_name=schema_name,
                columns=(
                    spec.get("columns")
                    if isinstance(spec.get("columns"), list)
                    else []
                ),
                grain=(
                    spec.get("grain") if isinstance(spec.get("grain"), list) else []
                ),
            )
            if failure is not None:
                return self.json_response(*failure)
            # A caller that only knows the statement still gets a typed draft: the
            # columns come from what the preview actually observed (P6-F02).
            if not (isinstance(spec.get("columns"), list) and spec.get("columns")):
                spec["columns"] = [
                    {
                        "name": entry.get("name"),
                        "type": entry.get("observed_type") or "other",
                    }
                    for entry in (profile.get("columns") or [])
                    if entry.get("name")
                ]

        status, body = call_xp_prompt(
            "POST",
            "/ai/datasets/drafts",
            payload={
                "superset_user_id": owner,
                "spec": spec,
                "profile": profile,
                "datasets": authorized_datasets(),
                "conversation_id": str(payload.get("conversation_id") or "").strip(),
                "job_id": str(payload.get("job_id") or "").strip(),
                "correlation_id": str(payload.get("correlation_id") or ""),
                "idempotency_key": str(payload.get("idempotency_key") or ""),
            },
        )
        if status not in (200, 201):
            return self.json_response(body, status)
        return self.json_response(
            {
                "draft": body.get("draft"),
                "quality": (body.get("draft") or {}).get("quality") or {},
                "profile": _profile_summary(profile),
            },
            201,
        )

    @expose("/api/datasets/drafts/<draft_id>/save/", methods=("POST",))
    @has_access_api
    def api_dataset_draft_save(self, draft_id: str) -> FlaskResponse:
        """Create the dataset through Superset's own command, then verify it (F06/F07).

        Authorization, schema detection, uniqueness and metadata writes are all
        Superset's: this view assembles the payload and confirms the result by reading
        the dataset back. A failure never leaves a half-object: the draft is scrapped
        and the failure is classified by the Phase 5 taxonomy (P6-F10).
        """
        owner = _current_user_id()
        if owner is None:
            return self.json_response(
                {"message": _("No authenticated user"), "severity": "danger"}, 401
            )
        status, body = call_xp_prompt(
            "GET",
            f"/ai/datasets/drafts/{draft_id}?superset_user_id={owner}&include_sql=true",
        )
        if status != 200:
            return self.json_response(body, status)
        draft = body.get("draft") or {}
        if draft.get("status") != "draft":
            return self.json_response(
                {
                    "message": _("This draft is %(status)s and cannot be saved again")
                    % {"status": draft.get("status")},
                    "severity": "warning",
                    "code": "not_open",
                },
                409,
            )
        spec = draft.get("spec") or {}
        database, failure = _dataset_database(spec)
        if failure is not None:
            return self.json_response(*failure)

        # Never create a second copy of something that already exists (P6-F09).
        matches = _dataset_equivalents(
            database.database_name,
            sql=str(draft.get("sql") or ""),
            table_name=str(spec.get("table") or ""),
        )
        if matches and not bool(_body().get("allow_duplicate")):
            _scrap_draft(owner, draft_id, "duplicate_dataset")
            return self.json_response(
                {
                    "message": _(
                        "An equivalent dataset already exists. Open it, or rename this "
                        "one to save a copy."
                    ),
                    "severity": "warning",
                    "code": "duplicate_dataset",
                    "matches": matches,
                },
                409,
            )

        try:
            dataset = _create_dataset(
                owner,
                database,
                draft,
                spec,
                schema_name=str(spec.get("schema") or "").strip(),
            )
        except Exception as exc:  # pylint: disable=broad-except
            hint, kind = _classify_failure(owner, "", f"{type(exc).__name__}: {exc}")
            _scrap_draft(owner, draft_id, f"save_failed:{kind}")
            logger.warning("XP Prompt dataset save failed: %s", type(exc).__name__)
            return self.json_response(
                {
                    "message": hint,
                    "severity": "danger",
                    "code": "dataset_save_failed",
                    "failure": kind,
                },
                422,
            )

        call_xp_prompt(
            "POST",
            f"/ai/datasets/drafts/{draft_id}/saved",
            payload={
                "superset_user_id": owner,
                "dataset_id": int(dataset.id),
                "owners": [
                    str(getattr(getattr(g, "user", None), "username", "") or "")
                ],
            },
        )
        verified = _verify_dataset(dataset.id)
        return self.json_response(
            {
                "dataset": verified,
                "blocks": _dataset_saved_blocks(verified, spec),
                "quality": draft.get("quality") or {},
            },
            201,
        )

    @expose("/api/datasets/drafts/<draft_id>/scrap/", methods=("POST",))
    @has_access_api
    def api_dataset_draft_scrap(self, draft_id: str) -> FlaskResponse:
        owner = _current_user_id()
        if owner is None:
            return self.json_response(
                {"message": _("No authenticated user"), "severity": "danger"}, 401
            )
        payload = _body()
        status, body = call_xp_prompt(
            "POST",
            f"/ai/datasets/drafts/{draft_id}/scrap",
            payload={
                "superset_user_id": owner,
                "reason": str(payload.get("reason") or "discarded"),
            },
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
                # "Regenerate" must reach the service: a cache hit is otherwise
                # indistinguishable from a fresh answer (P4-F11).
                "bypass_cache": bool(payload.get("regenerate")),
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
            "cache_status": str(candidate.get("cache") or ""),
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

        frame, duration_ms, error_text, repair, repair_attempts = _run_with_repair(
            owner=owner,
            conversation_id=conversation_id,
            question=question,
            expression=expression,
            database=database,
            schema_name=schema_name,
            context=context,
        )
        if error_text:
            hint, kind = _classify_failure(owner, conversation_id, error_text)
            return self._refuse(
                owner,
                conversation_id,
                (
                    {"message": hint, "severity": "danger", "failure": kind},
                    400,
                ),
                "execute",
                kind,
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
            *_repair_blocks(repair, repair_attempts),
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
            code="repaired" if repair is not None else "ok",
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
                "cache": candidate.get("cache"),
                "repair": _repair_summary(repair, repair_attempts),
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

        # The service publishes the safeguards it enforced, so this view does not
        # have to keep a second copy of them: the row cap can only narrow what the
        # caller asked for, and the concurrency limit is the service's number.
        safeguards = _safeguards(validated)
        if safeguards["max_rows"]:
            max_rows = min(max_rows, int(safeguards["max_rows"]))

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
        with _concurrency_slot(safeguards["max_concurrent"]) as admitted:
            if not admitted:
                logger.info(
                    "XP Prompt execution deferred: %s statements already running",
                    _ACTIVE_SQL,
                )
                return self.json_response(
                    {
                        "message": _(
                            "Too many AI queries are running; try again shortly"
                        ),
                        "severity": "warning",
                        "code": "sql_concurrency_exceeded",
                    },
                    429,
                )
            try:
                frame = database.get_df(validated["sql"], schema=schema_name)
            except Exception as exc:  # pylint: disable=broad-except
                logger.warning("XP Prompt execution failed: %s", type(exc).__name__)
                return self.json_response(
                    {
                        "message": _("The query could not be executed"),
                        "severity": "danger",
                    },
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
                # Reported, not applied here: the statement timeout is the
                # database engine's to enforce, and saying what is in force is
                # better than an operator assuming it (P10-F06).
                "limits": safeguards,
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
#: Repair-pattern transitions an operator may request. ``promote`` needs evidence;
#: ``quarantine`` is the immediate brake (P5-F09).
_PATTERN_ACTIONS = ("promote", "deprecate", "quarantine", "restore")
#: Bounds on the authorized slice: a prompt is not a schema dump.
MAX_SLICE_DATASETS = 50
MAX_SLICE_COLUMNS = 60
#: Charts considered for reuse when planning a dashboard. Bounded because the plan
#: only needs "is there already a chart of this family on this dataset".
MAX_PLAN_CHARTS = 200
#: The deterministic planner has the same cap. Repeating it here prevents a forged
#: preview request from turning one browser action into an unbounded query fan-out.
MAX_DASHBOARD_PREVIEW_SLOTS = 8

#: Most rows a preview may return. Execution is synchronous, so this is also the
#: practical limit on how much a single request can serialise.
MAX_PREVIEW_ROWS = 200
#: Fetch one sentinel row when a chart asks for more than the public preview cap, so
#: ``truncated`` is evidence rather than a guess. The sentinel never reaches the client.
MAX_CHART_QUERY_ROWS = MAX_PREVIEW_ROWS + 1
MAX_CELL_CHARS = 200
#: A virtual dataset's SQL is forwarded so the service can inline it; the service
#: enforces the same bound, this is only here to keep one request small.
MAX_DATASET_SQL_CHARS = 8_000
#: Rendered row-level predicates travel with the dataset; the service re-bounds and
#: parses them before they reach a statement.
MAX_RLS_CHARS = 2_000
#: A driver error message is bounded before it leaves the process; the service masks
#: it further before anything is stored.
MAX_ERROR_CHARS = 400
#: A plan probe is a read of the planner only, so its own output is bounded hard:
#: a pathological plan is refused by the service anyway, not summarised here.
MAX_PLAN_ROWS = 200
#: How many corrections one turn may try. The service owns the real budget; this is
#: only the loop's own backstop so a bad reply cannot spin here (P5-F06).
MAX_REPAIR_ROUNDS = 2
#: How many equivalent datasets are worth showing before the answer is "reuse one".
MAX_EQUIVALENTS = 5
#: A profile is computed over a bounded sample: aggregates only, never a value.
MAX_PROFILE_ROWS = 1_000
MAX_PROFILE_COLUMNS = 200
MAX_TITLE_CHARS = 256
MAX_DESCRIPTION_CHARS = 1_000
MAX_EXPRESSION_CHARS = 1_000
#: Rows read to decide cardinality before recommending a chart. Small on purpose: the
#: rules only need to know "a handful" from "thousands".
CHART_SAMPLE_ROWS = 200
#: The modern visualizations this frontend build registers. A deployment may override
#: the set with XPBUILDER_CHART_VIZ_TYPES; the configured denylist always wins.
DECLARED_CHART_VIZ: tuple[str, ...] = (
    "echarts_timeseries_line",
    "echarts_timeseries_bar",
    "echarts_timeseries_area",
    "echarts_timeseries_scatter",
    "pie",
    "heatmap_v2",
    "big_number_total",
    "table",
)


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
        "cache_status": extra.pop("cache_status", ""),
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
) -> tuple[Any, int, str]:
    """Run the statement on Superset's own connection and time it.

    Returns ``(frame, duration_ms, error_text)``; ``error_text`` is empty on success.
    The text is the driver's own message, bounded, and is sent to the service for
    classification — it is never stored or echoed to the browser verbatim.
    """
    started = time.monotonic()
    try:
        frame = database.get_df(sql, schema=schema_name)
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("XP Prompt execution failed: %s", type(exc).__name__)
        message = str(exc).strip()[:MAX_ERROR_CHARS]
        return (
            None,
            round((time.monotonic() - started) * 1000),
            f"{type(exc).__name__}: {message}",
        )
    return frame, round((time.monotonic() - started) * 1000), ""


def _classify_failure(
    owner: int, conversation_id: str, error_text: str
) -> tuple[str, str]:
    """Ask the service what kind of failure this is, and what to tell the user.

    The taxonomy lives in the service so the same failure is classified the same way
    for the audit, the repair budget and the message the user sees (P5-F01).
    """
    try:
        status, body = call_xp_prompt(
            "POST",
            "/ai/sql/failure",
            payload={
                "superset_user_id": owner,
                "conversation_id": conversation_id,
                "stage": "execute",
                "message": error_text,
            },
        )
        if status == 200:
            failure = body.get("failure") or {}
            hint = str(failure.get("hint") or "").strip()
            kind = str(failure.get("kind") or "execution_failed")
            if hint:
                return hint, kind
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning(
            "XP Prompt failure classification failed: %s", type(exc).__name__
        )
    return str(_("The query could not be executed")), "execution_failed"


def _repair_blocks(
    repair: dict[str, Any] | None, attempts: int
) -> list[dict[str, Any]]:
    """A repaired turn says so in the transcript: healed is not the same as clean."""
    if repair is None:
        return []
    return [
        {
            "type": "repair",
            "outcome": "fixed",
            "attempts": attempts,
            "notes": _(
                "The first statement failed; a validated %(source)s correction was "
                "re-run."
            )
            % {"source": str(repair.get("source") or "generated")},
        }
    ]


def _repair_summary(
    repair: dict[str, Any] | None, attempts: int
) -> dict[str, Any] | None:
    """What the browser needs to know about the repair, and nothing more."""
    if repair is None:
        return None
    return {
        "attempts": attempts,
        "source": repair.get("source"),
        "pattern_id": repair.get("pattern_id") or None,
    }


def _sql_digest(sql: str) -> str:
    """Whitespace-insensitive identity for "the same query" (never the text itself)."""
    text = " ".join((sql or "").split()).lower()
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text else ""


def _chart_catalog_payload() -> dict[str, Any]:
    """What this deployment can render: its frontend build, minus the denylist.

    The API process cannot enumerate the frontend's plugin registry, so the declared set
    is a deployment fact: ``XPBUILDER_CHART_VIZ_TYPES`` when set, otherwise the modern
    types this build ships. Superset's own backend registry is added on top, and the
    configured denylist always wins.
    """
    from flask import current_app

    from superset.viz import viz_types

    denied = [
        str(name)
        for name in (current_app.config.get("VIZ_TYPE_DENYLIST") or [])
        if name
    ]
    names = {str(name) for name in viz_types if name}
    declared = os.environ.get("XPBUILDER_CHART_VIZ_TYPES", "").strip()
    if declared:
        names |= {part.strip() for part in declared.split(",") if part.strip()}
    else:
        names |= set(DECLARED_CHART_VIZ)
    entries = [{"viz_type": name} for name in sorted(names) if name not in denied]
    return {"viz_types": entries, "denylist": denied}


def _chart_dataset(dataset_id: Any) -> tuple[Any, tuple[dict[str, Any], int] | None]:
    """Resolve the dataset and prove the caller may read it, or answer with why not."""
    from superset.daos.dataset import DatasetDAO

    try:
        identifier = int(dataset_id)
    except (TypeError, ValueError):
        return None, (
            {"message": _("dataset_id is required"), "severity": "danger"},
            400,
        )
    dataset = DatasetDAO.find_by_id(identifier)
    if dataset is None:
        return None, ({"message": _("Dataset not found"), "severity": "danger"}, 404)
    try:
        security_manager.raise_for_access(datasource=dataset)
    except SupersetSecurityException:
        return None, (
            {
                "message": _("You are not authorized for this dataset"),
                "severity": "danger",
                "code": "dataset_not_authorized",
            },
            403,
        )
    return dataset, None


def _chart_columns(dataset: Any) -> list[dict[str, Any]]:
    """The dataset's declared columns, in the shape the profiler expects."""
    return [
        {
            "name": column.column_name,
            "type": column.type or "",
            "role": "temporal" if column.is_dttm else "",
        }
        for column in (dataset.columns or [])[:MAX_PROFILE_COLUMNS]
    ]


def _chart_measured(dataset: Any) -> dict[str, Any]:
    """Cardinality and nulls from a bounded sample, so the rules are not guessing.

    One query, at most ``CHART_SAMPLE_ROWS`` rows, for a virtual dataset. A physical
    dataset is left unmeasured rather than issuing a scan whose shape we do not
    control — the rules then skip the checks that need a cardinality.
    """
    if not dataset.sql:
        return {}
    try:
        # The definition is the dataset's own, already validated when it was saved
        # (the guard refuses anything but a single read-only query), and it runs under
        # the caller's own credentials.
        frame = dataset.database.get_df(_chart_sample_sql(dataset.sql))
    except Exception as exc:  # pylint: disable=broad-except
        logger.info("XP Prompt chart sample unavailable: %s", type(exc).__name__)
        return {}
    profile = _profile(frame, columns=_chart_columns(dataset), grain=[])
    return {
        "columns": profile.get("columns") or [],
        "row_count": profile.get("row_count") or 0,
    }


def _chart_sample_sql(definition: str, limit: int = CHART_SAMPLE_ROWS) -> str:
    """Wrap a stored dataset definition in a bounded sample subquery."""
    return f"SELECT * FROM ({definition}) AS xp_s LIMIT {limit}"  # noqa: S608


def _chart_dataset_summary(dataset: Any) -> dict[str, Any]:
    return {
        "dataset_id": int(dataset.id),
        "name": dataset.table_name,
        "schema": dataset.schema or "",
        "database": getattr(dataset.database, "database_name", ""),
        "columns": len(dataset.columns or []),
    }


def _plan_datasets(
    dataset_ids: Any,
) -> tuple[list[dict[str, Any]], tuple[dict[str, Any], int] | None]:
    """The authorized datasets a dashboard may be planned from, optionally narrowed.

    The slice is the authority (``authorized_datasets`` is what Explore itself gates
    on), so asking for a dataset outside it is refused rather than silently ignored.
    """
    available = authorized_datasets()
    if dataset_ids in (None, ""):
        return available, None
    if not isinstance(dataset_ids, (list, tuple)):
        return [], (
            {"message": _("dataset_ids must be a list"), "severity": "danger"},
            400,
        )
    wanted = {int(item) for item in dataset_ids if isinstance(item, int)}
    if not wanted:
        return [], (
            {"message": _("dataset_ids must be a list of ids"), "severity": "danger"},
            400,
        )
    known = {int(dataset["dataset_id"]) for dataset in available}
    if not wanted <= known:
        return [], (
            {
                "message": _("You are not authorized for one of those datasets"),
                "severity": "danger",
                "code": "dataset_not_authorized",
            },
            403,
        )
    return [
        dataset for dataset in available if int(dataset["dataset_id"]) in wanted
    ], None


def _plan_dataset(dataset: dict[str, Any]) -> dict[str, Any]:
    """One authorized dataset in the shape the planner expects."""
    return {
        "id": int(dataset["dataset_id"]),
        "name": str(dataset.get("name") or ""),
        "columns": [
            {"name": str(column.get("name") or "")}
            for column in dataset.get("columns") or []
            if column.get("name")
        ],
    }


def _plan_charts(datasets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Saved charts on those datasets — the reuse candidates for the planner."""
    from superset.models.slice import Slice

    ids = [int(dataset["dataset_id"]) for dataset in datasets]
    if not ids:
        return []
    rows = (
        db.session.query(Slice)
        .filter(Slice.datasource_id.in_(ids))
        .order_by(Slice.id)
        .limit(MAX_PLAN_CHARTS)
        .all()
    )
    charts: list[dict[str, Any]] = []
    for chart in rows:
        try:
            params = json_loads(chart.params or "{}")
        except (TypeError, ValueError):
            params = {}
        charts.append(
            {
                "id": int(chart.id),
                "name": chart.slice_name or f"chart {chart.id}",
                "viz_type": str((params or {}).get("viz_type") or chart.viz_type or ""),
                "dataset_id": int(chart.datasource_id),
            }
        )
    return charts


def _plan_column_kinds(datasets: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    """Column kinds per dataset, so a native filter is only proposed where proven."""
    kinds: dict[str, dict[str, str]] = {}
    for dataset in datasets:
        kinds[str(int(dataset["dataset_id"]))] = {
            str(column.get("name")): (
                "temporal" if column.get("temporal") else "categorical"
            )
            for column in dataset.get("columns") or []
            if column.get("name")
        }
    return kinds


def _dashboard_preview_slots(
    slots: Any,
) -> tuple[list[dict[str, Any]], tuple[dict[str, Any], int] | None]:
    """Resolve and authorize a complete slot list before the first query runs."""
    if not isinstance(slots, list):
        return [], (
            {"message": _("slots must be a list"), "severity": "danger"},
            400,
        )
    if not slots:
        return [], (
            {"message": _("At least one slot is required"), "severity": "danger"},
            400,
        )
    if len(slots) > MAX_DASHBOARD_PREVIEW_SLOTS:
        return [], (
            {
                "message": _("Too many dashboard slots"),
                "severity": "danger",
                "code": "slot_budget",
            },
            422,
        )

    available_viz = {
        str(entry.get("viz_type") or "")
        for entry in _chart_catalog_payload().get("viz_types") or []
    }
    resolved: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in slots:
        if not isinstance(raw, dict):
            return [], (
                {"message": _("Each slot must be an object"), "severity": "danger"},
                400,
            )
        slot_id = str(raw.get("id") or "").strip()[:64]
        if not slot_id:
            return [], (
                {"message": _("Each slot needs an id"), "severity": "danger"},
                400,
            )
        if slot_id in seen:
            return [], (
                {
                    "message": _("A dashboard slot was repeated"),
                    "severity": "danger",
                    "code": "duplicate_slot",
                },
                422,
            )
        seen.add(slot_id)
        entry, failure = _dashboard_preview_slot(raw, slot_id, available_viz)
        if failure is not None:
            return [], failure
        resolved.append(entry)
    return resolved, None


def _dashboard_preview_slot(
    raw: dict[str, Any], slot_id: str, available_viz: set[str]
) -> tuple[dict[str, Any], tuple[dict[str, Any], int] | None]:
    """Resolve one already-unique slot without executing its query."""
    dataset, failure = _chart_dataset(raw.get("dataset_id"))
    if failure is not None:
        return {}, failure

    chart_id = raw.get("chart_id")
    if chart_id not in (None, ""):
        config, failure = _saved_dashboard_preview_config(chart_id, dataset, raw)
    else:
        config, failure = _generated_dashboard_preview_config(raw, available_viz)
    if failure is not None:
        return {}, failure

    title = str(config["title"])
    viz_type = str(config["viz_type"])
    chart = config.get("chart")
    normalized = {
        "id": slot_id,
        "title": title[:MAX_TITLE_CHARS] or _("Untitled chart"),
        "question": str(raw.get("question") or title).strip()[:MAX_TITLE_CHARS],
        "family": str(raw.get("family") or "").strip()[:32],
        "section": str(raw.get("section") or "detail").strip()[:32],
        "dataset_id": int(dataset.id),
        "dataset_name": str(dataset.table_name or "")[:MAX_TITLE_CHARS],
        "viz_type": viz_type[:80],
        "chart_id": int(chart.id) if chart is not None else None,
        "source": str(config["source"]),
    }
    return {
        "slot": normalized,
        "dataset": dataset,
        "draft": {"form_data": config["form_data"], "viz_type": viz_type},
    }, None


def _saved_dashboard_preview_config(
    chart_id: Any, dataset: Any, raw: dict[str, Any]
) -> tuple[dict[str, Any], tuple[dict[str, Any], int] | None]:
    """Load a saved chart's authoritative configuration and check chart access."""
    from superset.daos.chart import ChartDAO

    try:
        identifier = int(chart_id)
    except (TypeError, ValueError):
        return {}, ({"message": _("Invalid chart id"), "severity": "danger"}, 400)
    chart = ChartDAO.find_by_id(identifier, skip_base_filter=True)
    if chart is None:
        return {}, ({"message": _("Chart not found"), "severity": "danger"}, 404)
    if int(chart.datasource_id or 0) != int(dataset.id):
        return {}, (
            {
                "message": _("The chart does not use this dataset"),
                "severity": "danger",
                "code": "chart_dataset_mismatch",
            },
            422,
        )
    try:
        security_manager.raise_for_access(chart=chart)
    except SupersetSecurityException:
        return {}, (
            {
                "message": _("You are not authorized for this chart"),
                "severity": "danger",
                "code": "chart_not_authorized",
            },
            403,
        )
    try:
        form_data = dict(json_loads(chart.params or "{}") or {})
    except (TypeError, ValueError):
        return {}, (
            {
                "message": _("The saved chart configuration is invalid"),
                "severity": "danger",
                "code": "invalid_chart_config",
            },
            422,
        )
    form_data["slice_id"] = int(chart.id)
    return {
        "chart": chart,
        "form_data": form_data,
        "viz_type": str(form_data.get("viz_type") or chart.viz_type or "").strip(),
        "title": str(raw.get("title") or chart.slice_name or "").strip(),
        "source": "existing",
    }, None


def _generated_dashboard_preview_config(
    raw: dict[str, Any], available_viz: set[str]
) -> tuple[dict[str, Any], tuple[dict[str, Any], int] | None]:
    """Use a generated slot's validated plan configuration without persisting it."""
    candidate = raw.get("form_data")
    if not isinstance(candidate, dict) or not candidate:
        return {}, (
            {
                "message": _("An unsaved slot needs chart form data"),
                "severity": "danger",
                "code": "missing_form_data",
            },
            422,
        )
    form_data = dict(candidate)
    viz_type = str(
        raw.get("viz_type") or form_data.get("viz_type") or ""
    ).strip()
    if not viz_type or viz_type not in available_viz:
        return {}, (
            {
                "message": _("This visualization is not available"),
                "severity": "danger",
                "code": "viz_type_unavailable",
            },
            422,
        )
    return {
        "chart": None,
        "form_data": form_data,
        "viz_type": viz_type,
        "title": str(raw.get("title") or "").strip(),
        "source": "generate",
    }, None


def _chart_frame(
    dataset: Any, draft: dict[str, Any]
) -> tuple[Any, str | None]:
    """Run a draft's configuration through the chart-data path (P7-F06).

    Returns ``(frame, None)`` on success, or ``(None, error_text)`` with the exception's
    own text, bounded, for the Phase 5 taxonomy.
    """
    import pandas as pd

    from superset.commands.chart.data.get_data_command import ChartDataCommand
    from superset.common.query_context_factory import QueryContextFactory

    form_data = dict(draft.get("form_data") or {})
    form_data["datasource"] = f"{int(dataset.id)}__table"
    queries = [_chart_query(draft)]
    try:
        query_context = QueryContextFactory().create(
            datasource={"id": int(dataset.id), "type": "table"},
            queries=queries,
            form_data=form_data,
            force=False,
        )
        command = ChartDataCommand(query_context)
        command.validate()
        result = command.run()
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning(
            "XP Prompt chart render failed: %s: %s",
            type(exc).__name__,
            str(exc)[:200],
        )
        return None, f"{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS]
    rows: list[Any] = []
    columns: list[str] = []
    if isinstance(result, dict):
        queries_result = result.get("queries") or []
        if queries_result:
            query_result = queries_result[0]
            rows = list(query_result.get("data") or [])
            columns = [str(column) for column in query_result.get("colnames") or []]
    frame = pd.DataFrame(rows, columns=columns or None)
    return frame, None


def _chart_query(draft: dict[str, Any]) -> dict[str, Any]:
    """The one query a draft describes: its dimensions, its metrics and its cap."""
    form_data = draft.get("form_data") or {}
    columns: list[str] = []
    x_axis = form_data.get("x_axis")
    if isinstance(x_axis, str) and x_axis:
        columns.append(x_axis)
    for key in ("groupby", "all_columns", "columns"):
        configured = form_data.get(key) or []
        if isinstance(configured, str):
            configured = [configured]
        for entry in configured if isinstance(configured, list | tuple) else []:
            if isinstance(entry, str) and entry not in columns:
                columns.append(entry)
    raw_metrics = form_data.get("metrics") or []
    metrics = (
        list(raw_metrics)
        if isinstance(raw_metrics, list | tuple)
        else [raw_metrics]
    )
    single = form_data.get("metric")
    if single:
        metrics = [*metrics, single]
    try:
        requested_limit = int(form_data.get("row_limit") or MAX_PREVIEW_ROWS)
    except (TypeError, ValueError):
        requested_limit = MAX_PREVIEW_ROWS
    return {
        "columns": columns,
        "metrics": metrics,
        "row_limit": max(1, min(requested_limit, MAX_CHART_QUERY_ROWS)),
        "order_desc": True,
        "filters": list(form_data.get("adhoc_filters") or [])
        if isinstance(form_data.get("adhoc_filters") or [], list | tuple)
        else [],
    }


def _create_chart(owner: int, dataset: Any, draft: dict[str, Any]) -> Any:
    """Create the chart through Superset's command, so its own validation applies."""
    from superset.commands.chart.create import CreateChartCommand

    data = {
        "slice_name": str(draft.get("title") or "")[:MAX_TITLE_CHARS],
        "viz_type": str(draft.get("viz_type") or ""),
        "datasource_type": "table",
        "datasource_id": int(dataset.id),
        "params": json_dumps(dict(draft.get("form_data") or {})),
        "description": str(draft.get("description") or "")[:MAX_DESCRIPTION_CHARS],
        "owners": [int(owner)],
        "dashboards": [],
    }
    return CreateChartCommand(data).run()


def _chart_record(chart: Any) -> dict[str, Any]:
    return {
        "slice_id": int(chart.id),
        "name": chart.slice_name,
        "viz_type": chart.viz_type,
        "dataset_id": int(chart.datasource_id) if chart.datasource_id else None,
        "verified": bool(chart.viz_type),
    }


def _chart_saved_blocks(chart: Any) -> list[dict[str, Any]]:
    return [
        {"type": "warnings", "items": [_("The chart was saved to Superset.")]},
        {
            "type": "links",
            "items": [
                {
                    "label": _("Open the chart"),
                    "entity_type": "chart",
                    "entity_id": str(int(chart.id)),
                }
            ],
        },
    ]


def _published_dashboard_blocks(record: dict[str, Any]) -> list[dict[str, Any]]:
    """A published dashboard is a link, not a summary: the object exists."""
    dashboard_id = record.get("dashboard_id")
    if not dashboard_id:
        return []
    return [
        {
            "type": "warnings",
            "items": [_("The dashboard was published to Superset.")],
        },
        {
            "type": "links",
            "items": [
                {
                    "label": _("Open the dashboard"),
                    "entity_type": "dashboard",
                    "entity_id": str(int(dashboard_id)),
                }
            ],
        },
    ]


#: The modes a publish may declare, and the ones that act on an existing dashboard.
_PUBLISH_MODES = frozenset({MODE_CREATE, MODE_UPDATE, MODE_DUPLICATE})
_PUBLISH_MODES_WITH_TARGET = frozenset({MODE_UPDATE, MODE_DUPLICATE})

#: Query parameters a usage read understands. Anything else is dropped: a proxy
#: that forwarded the whole query string would let a caller reach service
#: parameters this view never intended to expose.
_USAGE_PARAMS = ("group_by", "since", "module", "limit", "provider", "model", "key")


def _actor_label() -> str:
    """Who is making the change, for the audit row.

    Taken from the session rather than the request body: a setting audit that
    records whatever name the client supplies is decoration, not accountability.
    """
    try:
        user = g.user
        username = getattr(user, "username", None) or ""
        if username:
            return str(username)[:128]
        return str(getattr(user, "id", "") or "")[:128]
    except Exception:  # pragma: no cover - defensive
        return ""


def _usage_query() -> str:
    """The usage filters the browser may pass, as a query string."""
    pairs: list[str] = []
    for key in _USAGE_PARAMS:
        for value in request.args.getlist(key):
            if value:
                pairs.append(f"{key}={quote(str(value), safe='')}")
    tenant = request.args.get("tenant")
    if tenant:
        pairs.append(f"tenant={quote(str(tenant), safe='')}")
    return "&".join(pairs)


def _publish_intent(
    owner: int,
    mode: str,
    payload: dict[str, Any],
    resolved: list[dict[str, Any]],
    target: Any,
) -> dict[str, Any]:
    """What the caller asked to publish, plus the facts only this process has.

    The service decides policy, but it cannot see Superset: the live layout and the
    capabilities of this build are supplied here, so a drift check and an access
    decision are made against reality rather than a guess (P8-F09/F11).
    """
    return {
        "superset_user_id": owner,
        "mode": mode,
        "title": str(payload.get("title") or ""),
        "goal": str(payload.get("goal") or ""),
        "slots": [entry["slot"] for entry in resolved],
        "owners": payload.get("owners") or [],
        "roles": payload.get("roles") or [],
        "visibility": str(payload.get("visibility") or "private"),
        "guest_access": bool(payload.get("guest_access")),
        "dashboard_id": int(target.id) if target is not None else None,
        "expected_fingerprint": str(payload.get("expected_fingerprint") or ""),
        "expected_version": payload.get("expected_version"),
        "force": bool(payload.get("force")),
        "idempotency_key": str(payload.get("idempotency_key") or ""),
        "conversation_id": payload.get("conversation_id"),
        "job_id": payload.get("job_id"),
        "live_position": dashboard_publish.live_position(target),
        "policy": dashboard_publish.policy_payload(),
    }


def _publish_replay(body: dict[str, Any]) -> tuple[dict[str, Any], int] | None:
    """A repeated request answers with what the first one produced (P8-F08)."""
    if not body.get("replayed"):
        return None
    if body.get("status") == "published":
        return (
            {
                "publish": body,
                "dashboard_id": body.get("dashboard_id"),
                "replayed": True,
                "blocks": _published_dashboard_blocks(body),
            },
            200,
        )
    return (
        {
            "message": _(
                "This publish already started and has not finished; wait for it "
                "rather than starting a second one"
            ),
            "severity": "warning",
            "code": "publish_in_flight",
            "publish": body,
        },
        409,
    )


def _dashboard_columns(
    resolved: list[dict[str, Any]],
) -> dict[str, dict[str, str]]:
    """Column kinds per dataset, so a native filter is only emitted where proven."""
    kinds: dict[str, dict[str, str]] = {}
    for entry in resolved:
        dataset = entry["dataset"]
        kinds[str(int(dataset.id))] = {
            str(column.column_name): "temporal" if column.is_dttm else "categorical"
            for column in (dataset.columns or [])
            if column.column_name
        }
    return kinds


def _publish_objects(
    owner: int,
    mode: str,
    publisher: Any,
    resolved: list[dict[str, Any]],
    target: Any,
    payload: dict[str, Any],
    publish_id: str,
    decision: dict[str, Any],
) -> tuple[dict[str, Any], Any]:
    """Create what the plan still needs, then write the dashboard itself.

    A generated slot becomes a real chart first, because position metadata can only
    reference a chart that exists. A duplicate creates nothing: it copies its original,
    charts included, which is what "duplicate" means to a user (P8-F08).
    """
    title = str(payload.get("title") or "")[:MAX_TITLE_CHARS]
    owners = [int(value) for value in decision.get("owners") or [owner]]
    roles = [int(value) for value in decision.get("roles") or []]

    if mode == MODE_DUPLICATE:
        copy = publisher.duplicate_dashboard(
            target,
            {
                "dashboard_title": title,
                "json_metadata": target.json_metadata or "{}",
                "owners": owners,
            },
        )
        return {}, dashboard_publish.live_position(copy)

    for entry in resolved:
        if entry["slot"].get("chart_id"):
            continue
        draft = dict(entry["draft"])
        draft["title"] = entry["slot"]["title"]
        chart = publisher.ensure_chart(entry["dataset"], draft)
        entry["slot"]["chart_id"] = int(chart.id)
        entry["slot"]["source"] = "generated"

    status, layout = call_xp_prompt(
        "POST",
        "/ai/dashboards/layout",
        payload={
            "superset_user_id": owner,
            "title": title,
            "slots": [entry["slot"] for entry in resolved],
            "columns": _dashboard_columns(resolved),
            "max_filters": payload.get("max_filters"),
        },
    )
    if status != 200 or not layout.get("valid"):
        problems = (
            layout.get("problems")
            or layout.get("validation")
            or [layout.get("message") or _("the layout could not be built")]
        )
        raise dashboard_publish.PublishBlockedError(
            "layout_invalid",
            _("The layout cannot be published: %(problem)s")
            % {"problem": str(problems[0])[:200]},
        )

    position = layout.get("position") or {}
    metadata = {
        "native_filters": layout.get("native_filters") or [],
        "cross_filters_enabled": bool(layout.get("native_filters")),
    }
    body = dashboard_publish.dashboard_payload(
        title=title,
        position_json=json_dumps(position),
        metadata=metadata,
        owners=owners,
        roles=roles,
        slug=str(payload.get("slug") or ""),
        description=str(payload.get("description") or ""),
    )
    if mode == MODE_CREATE:
        publisher.create_dashboard(body)
    else:
        publisher.update_dashboard(target, body)
    logger.info(
        "dashboard_publish wrote mode=%s publish=%s slots=%d filters=%d",
        mode,
        publish_id,
        len(resolved),
        len(metadata["native_filters"]),
    )
    return metadata, position


def _scrap_chart_draft(owner: int, draft_id: str, reason: str) -> None:
    try:
        call_xp_prompt(
            "POST",
            f"/ai/charts/drafts/{draft_id}/scrap",
            payload={"superset_user_id": owner, "reason": reason},
        )
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("XP Prompt chart draft scrap failed: %s", type(exc).__name__)


def _dataset_equivalents(
    database_name: str, *, sql: str = "", table_name: str = ""
) -> list[dict[str, Any]]:
    """Existing datasets the caller may already use for this definition (P6-F01).

    The search runs over the *authorized* slice, never the metadata table directly, so
    an equivalence check cannot disclose a dataset the caller could not open.
    """
    digest = _sql_digest(sql)
    wanted_table = (table_name or "").strip().lower()
    matches: list[dict[str, Any]] = []
    for dataset in authorized_datasets():
        if str(dataset.get("database") or "") != database_name:
            continue
        stored_sql = str(dataset.get("sql") or "")
        stored_table = str(dataset.get("name") or "").strip().lower()
        if digest and stored_sql and _sql_digest(stored_sql) == digest:
            kind = "virtual"
        elif (
            not digest
            and wanted_table
            and stored_table == wanted_table
            and not stored_sql
        ):
            kind = "physical"
        else:
            continue
        matches.append(
            {
                "dataset_id": dataset.get("dataset_id"),
                "name": dataset.get("name"),
                "schema": dataset.get("schema") or "",
                "database": database_name,
                "kind": kind,
            }
        )
        if len(matches) >= MAX_EQUIVALENTS:
            break
    return matches


def _dataset_database(
    spec: dict[str, Any]
) -> tuple[Any, tuple[dict[str, Any], int] | None]:
    """Resolve the target database from the *authorized* slice, never from an id."""
    name = str((spec or {}).get("database") or "").strip()
    if not name:
        return None, ({"message": _("database is required"), "severity": "danger"}, 400)
    allowed = {
        str(dataset.get("database") or "") for dataset in authorized_datasets()
    }
    if name not in allowed:
        return None, (
            {
                "message": _("You are not authorized for that database"),
                "severity": "danger",
                "code": "database_not_authorized",
            },
            403,
        )
    database = (
        db.session.query(Database)
        .filter(Database.database_name == name)
        .one_or_none()
    )
    if database is None:
        return None, ({"message": _("Database not found"), "severity": "danger"}, 404)
    return database, None


def _dataset_preview(
    owner: int,
    database: Any,
    *,
    sql: str,
    schema_name: str | None,
    columns: list[Any],
    grain: list[Any],
) -> tuple[dict[str, Any], tuple[dict[str, Any], int] | None]:
    """Measure a definition through the guarded path (P6-F03).

    Re-validation, Superset's own authorization, and the capped execution are the same
    three steps a SQL turn takes: a dataset draft may not be built from a statement that
    was never safe to run.
    """
    validate_status, validated = call_xp_prompt(
        "POST",
        "/ai/sql/validate",
        payload={
            "superset_user_id": owner,
            "sql": sql,
            "database": database.database_name,
            "schema": schema_name,
            "max_rows": MAX_PREVIEW_ROWS,
            "datasets": authorized_datasets(),
        },
    )
    if validate_status != 200:
        return {}, (validated, validate_status)
    try:
        security_manager.raise_for_access(
            database=database, sql=validated["sql"], schema=schema_name
        )
    except SupersetSecurityException:
        return {}, (
            {"message": _("Not authorized to run this query"), "severity": "danger"},
            403,
        )
    frame, _duration_ms, error_text = _execute(database, validated["sql"], schema_name)
    if error_text:
        hint, kind = _classify_failure(owner, "", error_text)
        return {}, (
            {"message": hint, "severity": "danger", "failure": kind},
            400,
        )
    return _profile(frame, columns=columns, grain=grain), None


def _profile(
    frame: Any, *, columns: list[Any], grain: list[Any]
) -> dict[str, Any]:
    """What the sample says about the data: nulls, cardinality, types, grain.

    Only aggregates leave this function — no value and no row. The service turns them
    into quality warnings (P6-F04).
    """
    sample = frame.head(MAX_PROFILE_ROWS)
    row_count = int(len(frame))
    declared = {
        str(entry.get("name") or "").lower(): str(entry.get("type") or "").lower()
        for entry in columns
        if isinstance(entry, dict) and entry.get("name")
    }
    entries: list[dict[str, Any]] = []
    for name in list(sample.columns)[:MAX_PROFILE_COLUMNS]:
        series = sample[name]
        null_count = int(series.isna().sum())
        try:
            distinct = int(series.nunique(dropna=True))
        except TypeError:  # unhashable cell values
            distinct = 0
        observed = _observed_type(series)
        entry: dict[str, Any] = {
            "name": str(name),
            "null_count": null_count,
            "distinct_count": distinct,
            "observed_type": observed,
        }
        wanted = declared.get(str(name).lower(), "")
        if wanted in {"date", "datetime"} and observed != "datetime":
            invalid = _invalid_dates(series)
            if invalid is not None:
                entry["invalid_dates"] = max(0, invalid - null_count)
        entries.append(entry)

    duplicate_grain = 0
    wanted_grain = [str(name) for name in grain if str(name or "").strip()]
    subset = [name for name in wanted_grain if name in sample.columns]
    if wanted_grain and len(subset) == len(wanted_grain) and subset:
        duplicate_grain = int(sample.duplicated(subset=subset).sum())
    return {
        "row_count": row_count,
        "sampled": row_count > len(sample),
        "duplicate_grain": duplicate_grain,
        "columns": entries,
    }


def _profile_summary(profile: dict[str, Any]) -> dict[str, Any]:
    """The preview the browser may see: counts, never values."""
    return {
        "row_count": profile.get("row_count", 0),
        "sampled": bool(profile.get("sampled")),
        "duplicate_grain": profile.get("duplicate_grain", 0),
        "columns": len(profile.get("columns") or []),
    }


def _observed_type(series: Any) -> str:
    """What the sample actually looks like, in the service's small vocabulary."""
    import pandas as pd

    kind = getattr(getattr(series, "dtype", None), "kind", "")
    if kind in {"i", "u"}:
        return "integer"
    if kind == "f":
        return "decimal"
    if kind == "b":
        return "boolean"
    if kind in {"M", "m"}:
        return "datetime"
    values = series.dropna().head(50)
    if values.empty:
        return "other"
    try:
        pd.to_numeric(values)
    except (TypeError, ValueError):
        return "text"
    return "decimal"


def _invalid_dates(series: Any) -> int | None:
    """How many non-null values fail to parse as a date, when that is answerable."""
    import pandas as pd

    values = series.dropna().head(MAX_PROFILE_ROWS)
    if values.empty:
        return 0
    try:
        parsed = pd.to_datetime(values, errors="coerce", format="mixed")
    except (TypeError, ValueError):
        return None
    return int(parsed.isna().sum())


def _create_dataset(
    owner: int,
    database: Any,
    draft: dict[str, Any],
    spec: dict[str, Any],
    *,
    schema_name: str,
) -> Any:
    """Create the dataset through Superset's command, then add declared metrics.

    The command performs uniqueness, table/SQL existence and `raise_for_access`, and
    `fetch_metadata()` detects the columns — so the object this returns is the one
    Superset itself considers valid.
    """
    from superset.commands.dataset.create import CreateDatasetCommand
    from superset.daos.dataset import DatasetDAO

    users = []
    for name in spec.get("owners") or []:
        user = security_manager.find_user(username=str(name))
        if user is not None:
            users.append(int(user.id))
    if not users:
        users = [int(owner)]

    statement = str(draft.get("sql") or "").strip()
    data: dict[str, Any] = {
        "database": int(database.id),
        "schema": schema_name or None,
        "table_name": str(spec.get("table") or spec.get("name") or draft.get("name")),
        "sql": statement or None,
        "owners": users,
        "description": str(spec.get("description") or "")[:MAX_DESCRIPTION_CHARS],
    }
    temporal = str(spec.get("temporal_column") or "").strip()
    if temporal:
        data["main_dtc"] = temporal
    dataset = CreateDatasetCommand(data).run()

    metrics = [
        {
            "metric_name": str(metric.get("name") or "")[:255],
            "expression": str(metric.get("expression") or "")[:MAX_EXPRESSION_CHARS],
            "description": str(metric.get("description") or "")[:255] or None,
        }
        for metric in (spec.get("metrics") or [])
        if isinstance(metric, dict) and metric.get("name") and metric.get("expression")
    ]
    if metrics:
        DatasetDAO.update_metrics(dataset, metrics)
        db.session.commit()
    return dataset


def _verify_dataset(dataset_id: int) -> dict[str, Any]:
    """Read the saved dataset back: columns detected, metrics attached (P6-F07)."""
    from superset.daos.dataset import DatasetDAO

    dataset = DatasetDAO.find_by_id(int(dataset_id))
    if dataset is None:  # pragma: no cover - deleted between create and read
        return {"dataset_id": int(dataset_id), "verified": False, "columns": 0}
    columns = [column.column_name for column in (dataset.columns or [])]
    metrics = [metric.metric_name for metric in (dataset.metrics or [])]
    return {
        "dataset_id": int(dataset.id),
        "name": dataset.table_name,
        "schema": dataset.schema or "",
        "database": getattr(dataset.database, "database_name", ""),
        "sql": bool(dataset.sql),
        "columns": len(columns),
        "column_names": columns[:MAX_SLICE_COLUMNS],
        "metrics": metrics,
        "owners": [getattr(owner, "username", "") for owner in (dataset.owners or [])],
        "verified": bool(columns),
    }


def _dataset_saved_blocks(
    verified: dict[str, Any], spec: dict[str, Any]
) -> list[dict[str, Any]]:
    """What the transcript records after a save: the object, and how to open it."""
    notes = [
        _("%(count)s columns were detected.")
        % {"count": verified.get("columns", 0)},
    ]
    if not verified.get("verified"):
        notes.append(
            _(
                "Superset could not detect columns; check the definition in the "
                "dataset editor."
            )
        )
    if spec.get("metrics"):
        notes.append(
            _("%(count)s metric(s) were added.")
            % {"count": len(verified.get("metrics") or [])}
        )
    return [
        {"type": "warnings", "items": notes},
        {
            "type": "links",
            "items": [
                {
                    "label": _("Open the dataset"),
                    "entity_type": "dataset",
                    "entity_id": str(verified.get("dataset_id") or ""),
                }
            ],
        },
    ]


def _scrap_draft(owner: int, draft_id: str, reason: str) -> None:
    """Close a draft nothing was created from; never let this raise into the turn."""
    try:
        call_xp_prompt(
            "POST",
            f"/ai/datasets/drafts/{draft_id}/scrap",
            payload={"superset_user_id": owner, "reason": reason},
        )
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("XP Prompt draft scrap failed: %s", type(exc).__name__)


def _run_with_repair(
    *,
    owner: int,
    conversation_id: str,
    question: str,
    expression: str,
    database: Any,
    schema_name: str | None,
    context: dict[str, Any],
) -> tuple[Any, int, str, dict[str, Any] | None, int]:
    """Execute, and if that fails, try to heal it — within the service's budget.

    Returns ``(frame, duration_ms, error_text, proposal, attempts)``. ``context`` is
    updated in place with the statement that finally ran, so the audit and the stored
    turn describe what actually executed rather than what was first proposed.
    """
    frame, duration_ms, error_text = _execute(database, expression, schema_name)
    proposal: dict[str, Any] | None = None
    attempts = 0
    spent_ms = 0
    while error_text and attempts < MAX_REPAIR_ROUNDS:
        attempts += 1
        next_proposal = _repair(
            owner=owner,
            conversation_id=conversation_id,
            question=question,
            failed_sql=expression,
            error_text=error_text,
            database=database,
            schema_name=schema_name,
            attempts=attempts - 1,
            spent_ms=spent_ms + duration_ms,
        )
        if next_proposal is None:
            break
        proposal = next_proposal
        expression = str(proposal.get("sql") or "")
        context["sql"] = expression
        context["dialect"] = str(
            proposal.get("dialect") or context.get("dialect") or ""
        )
        frame, repaired_ms, repaired_error = _execute(database, expression, schema_name)
        duration_ms += repaired_ms
        spent_ms += repaired_ms
        _record_repair_outcome(
            owner=owner,
            conversation_id=conversation_id,
            proposal=proposal,
            succeeded=not repaired_error,
            sql=expression,
            dialect=context["dialect"],
        )
        error_text = repaired_error
    return frame, duration_ms, error_text, proposal, attempts


def _repair(
    *,
    owner: int,
    conversation_id: str,
    question: str,
    failed_sql: str,
    error_text: str,
    database: Any,
    schema_name: str | None,
    attempts: int,
    spent_ms: int,
) -> dict[str, Any] | None:
    """Ask the service for a correction, then re-run the whole Phase 4 pipeline.

    The proposal that comes back has already passed the guard and the semantic check.
    It is *not* trusted on that strength: authorization, the cost gate, execution and
    result validation all run again here, exactly as they do for a first attempt —
    a repair may not bypass security (P5-F05). ``None`` means "stop trying".
    """
    status, proposal = call_xp_prompt(
        "POST",
        "/ai/sql/repair",
        payload={
            "superset_user_id": owner,
            "conversation_id": conversation_id,
            "question": question,
            "sql": failed_sql,
            "message": error_text,
            "stage": "execute",
            "database": database.database_name,
            "max_rows": MAX_PREVIEW_ROWS,
            "attempts": attempts,
            "elapsed_ms": spent_ms,
            "datasets": authorized_datasets(),
        },
    )
    if status != 200 or not isinstance(proposal, dict):
        logger.info("XP Prompt repair unavailable (%s)", status)
        return None
    if not proposal.get("allowed"):
        logger.info("XP Prompt repair not allowed: %s", proposal.get("reason"))
        return None
    expression = str(proposal.get("sql") or "").strip()
    if not expression:
        return None
    dialect = str(proposal.get("dialect") or "")

    failure = _authorize(database, expression, schema_name)
    if failure is not None:
        _record_repair_outcome(
            owner=owner,
            conversation_id=conversation_id,
            proposal=proposal,
            succeeded=False,
            sql=expression,
            dialect=dialect,
        )
        return None
    failure, _plan = _cost_gate(owner, database, expression, schema_name)
    if failure is not None:
        _record_repair_outcome(
            owner=owner,
            conversation_id=conversation_id,
            proposal=proposal,
            succeeded=False,
            sql=expression,
            dialect=dialect,
        )
        return None
    return proposal


def _record_repair_outcome(
    *,
    owner: int,
    conversation_id: str,
    proposal: dict[str, Any],
    succeeded: bool,
    sql: str,
    dialect: str,
) -> None:
    """Tell the service how the repair ended, so the knowledge can catch up.

    Bookkeeping must never break the turn: a failed call here is logged and swallowed.
    """
    try:
        call_xp_prompt(
            "POST",
            "/ai/sql/repair/outcome",
            payload={
                "superset_user_id": owner,
                "conversation_id": conversation_id,
                "failure": proposal.get("failure") or {},
                "succeeded": succeeded,
                "fix_fingerprint": proposal.get("fix_fingerprint") or "",
                "pattern_id": proposal.get("pattern_id") or "",
                "sql": sql,
                "dialect": dialect,
                "datasets": authorized_datasets(),
            },
        )
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("XP Prompt repair outcome not recorded: %s", type(exc).__name__)


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
