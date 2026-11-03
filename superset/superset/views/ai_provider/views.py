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
"""AI Provider settings — page plus an authorized proxy to the XP Prompt API.

XPBuilder keeps Superset as the authorization authority. The React settings page
is same-origin and talks only to this view; this view checks the Flask-AppBuilder
permission and then forwards the call to the dedicated XP Prompt service, signing
it with an internal token derived from the root secret. Provider keys are sealed
inside the XP Prompt database, are never returned by any endpoint, and never
reach the browser.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any

from flask import request
from flask_appbuilder import expose
from flask_appbuilder.security.decorators import has_access, has_access_api
from flask_babel import gettext as _

from superset.superset_typing import FlaskResponse
from superset.views.base import BaseSupersetView

logger = logging.getLogger(__name__)

#: Must stay in sync with ``services/xp_prompt/xp_prompt/security.py``.
#: ``tests/contract/check_compatibility.py`` asserts both salt values match.
INTERNAL_TOKEN_SALT = b"xp-prompt-internal-admin-v1"

DEFAULT_XP_PROMPT_API_URL = "http://xp-prompt-api:8090"
PROXY_TIMEOUT_SECONDS = 30
ADMIN_TOKEN_HEADER = "X-XP-Prompt-Token"


def _service_secret() -> str:
    return (
        os.environ.get("XP_PROMPT_SERVICE_SECRET", "").strip()
        or os.environ.get("SUPERSET_SECRET_KEY", "").strip()
    )


def internal_admin_token() -> str:
    """Derive the token the XP Prompt API accepts on admin endpoints."""
    secret = _service_secret()
    if not secret:
        raise RuntimeError(
            "SUPERSET_SECRET_KEY must be set to manage AI providers"
        )
    return hmac.new(
        secret.encode("utf-8"), INTERNAL_TOKEN_SALT, hashlib.sha256
    ).hexdigest()


def _api_base_url() -> str:
    return (
        os.environ.get("XP_PROMPT_API_URL", "").strip() or DEFAULT_XP_PROMPT_API_URL
    ).rstrip("/")


def call_xp_prompt(
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Call the XP Prompt admin API and return its status and JSON body."""
    body = None
    headers = {"Accept": "application/json", ADMIN_TOKEN_HEADER: internal_admin_token()}
    if payload is not None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"

    upstream = urllib.request.Request(
        f"{_api_base_url()}{path}",
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(upstream, timeout=PROXY_TIMEOUT_SECONDS) as response:
            return response.status, _decode(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _decode(exc.read())
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.warning("XP Prompt AI service is unreachable: %s", type(exc).__name__)
        return 503, {
            "message": _(
                "The XP Prompt AI service is not reachable. Check that the "
                "xp-prompt-api container is running."
            ),
            "severity": "danger",
        }


def _decode(raw: bytes) -> dict[str, Any]:
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"message": _("Unexpected response from the AI service")}
    return decoded if isinstance(decoded, dict) else {"result": decoded}


class AIProviderView(BaseSupersetView):
    """Provider configuration for the XP Prompt AI features."""

    route_base = "/aiprovider"
    class_permission_name = "AIProvider"

    @expose("/")
    @has_access
    def settings(self) -> FlaskResponse:
        return super().render_app_template()

    @expose("/list/")
    @has_access
    def list(self) -> FlaskResponse:  # noqa: A003 - FAB route name
        """Alias kept so an old bookmark or menu href still resolves."""
        return super().render_app_template()

    # --- Proxy API -----------------------------------------------------

    @expose("/api/providers/", methods=("GET",))
    @has_access_api
    def api_providers(self) -> FlaskResponse:
        tenant = request.args.get("tenant", "")
        path = "/ai/providers" + (f"?tenant={tenant}" if tenant else "")
        status, body = call_xp_prompt("GET", path)
        return self.json_response(body, status)

    @expose("/api/providers/types/", methods=("GET",))
    @has_access_api
    def api_provider_types(self) -> FlaskResponse:
        status, body = call_xp_prompt("GET", "/ai/providers/types")
        return self.json_response(body, status)

    @expose("/api/providers/", methods=("POST",))
    @has_access_api
    def api_provider_create(self) -> FlaskResponse:
        payload = request.get_json(force=True, silent=True) or {}
        status, body = call_xp_prompt("POST", "/ai/providers", payload=payload)
        return self.json_response(body, status)

    @expose("/api/providers/<provider_id>/", methods=("PUT",))
    @has_access_api
    def api_provider_update(self, provider_id: str) -> FlaskResponse:
        payload = request.get_json(force=True, silent=True) or {}
        status, body = call_xp_prompt(
            "PUT", f"/ai/providers/{provider_id}", payload=payload
        )
        return self.json_response(body, status)

    @expose("/api/providers/<provider_id>/", methods=("DELETE",))
    @has_access_api
    def api_provider_delete(self, provider_id: str) -> FlaskResponse:
        status, body = call_xp_prompt("DELETE", f"/ai/providers/{provider_id}")
        return self.json_response(body, status)

    @expose("/api/providers/<provider_id>/test/", methods=("POST",))
    @has_access_api
    def api_provider_test(self, provider_id: str) -> FlaskResponse:
        status, body = call_xp_prompt(
            "POST", f"/ai/providers/{provider_id}/test"
        )
        return self.json_response(body, status)
