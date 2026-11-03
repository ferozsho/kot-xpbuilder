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
from typing import Any

from flask import g, request
from flask_appbuilder import expose
from flask_appbuilder.security.decorators import has_access, has_access_api
from flask_babel import gettext as _

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


#: Browser-facing verb -> XP Prompt endpoint verb. Only these four exist upstream.
_JOB_ACTIONS = {
    "cancel": "cancel",
    "retry": "retry",
    "confirm": "confirm",
    "reject": "reject",
}


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
