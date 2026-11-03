"""HTTP surface for the KOT5 XP Prompt API process.

Three route groups live here:

* ``/health/*`` — unauthenticated liveness/readiness probes.
* ``/ai/providers`` — admin-managed provider configuration. The XPBuilder React
  settings page never calls these directly: the Superset backend proxies them
  after enforcing Flask-AppBuilder authorization and signs each request with the
  internal service token derived from the root secret.
* ``/ai/providers/<id>/test`` — redacted provider connection test.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from functools import wraps
from typing import Any

from flask import Flask, Response, jsonify, request

from xp_prompt import __version__
from xp_prompt.config import Settings
from xp_prompt.health import readiness
from xp_prompt.providers import registry
from xp_prompt.providers.types import Message, ProviderError, ProviderRequest
from xp_prompt.security import constant_time_equals, internal_admin_token

ADMIN_TOKEN_HEADER = "X-XP-Prompt-Token"
TEST_PROMPT = "Reply with the single word OK."
TEST_MAX_TOKENS = 16


def _admin_error(message: str, status: int) -> tuple[Response, int]:
    return jsonify({"message": message, "severity": "danger"}), status


def _error_class(exc: BaseException) -> str:
    return type(exc).__name__


def create_app(settings: Settings | None = None) -> Flask:
    """Create the XP Prompt API application."""
    app = Flask(__name__)
    resolved_settings = settings or Settings.from_env()

    def require_admin_token(view: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(view)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                expected = internal_admin_token(resolved_settings.service_secret)
            except RuntimeError as exc:
                return _admin_error(str(exc), 503)
            supplied = request.headers.get(ADMIN_TOKEN_HEADER, "")
            if not supplied or not constant_time_equals(supplied, expected):
                return _admin_error("Administrator token required", 401)
            return view(*args, **kwargs)

        return wrapper

    @app.get("/health/live")
    def health_live():
        return jsonify({"service": "xp-prompt-api", "status": "alive"}), 200

    @app.get("/health/ready")
    def health_ready():
        ready, dependencies = readiness(resolved_settings)
        return (
            jsonify(
                {
                    "service": "xp-prompt-api",
                    "status": "ready" if ready else "unavailable",
                    "version": __version__,
                    "dependencies": dependencies,
                }
            ),
            200 if ready else 503,
        )

    @app.get("/ai/providers/types")
    @require_admin_token
    def provider_types():
        return jsonify({"result": registry.provider_types_payload()}), 200

    @app.get("/ai/providers")
    @require_admin_token
    def provider_list():
        tenant_key = request.args.get("tenant") or registry.DEFAULT_TENANT_KEY
        try:
            records = registry.list_providers(
                resolved_settings, tenant_key=tenant_key
            )
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(f"Could not load providers ({_error_class(exc)})", 503)
        return (
            jsonify(
                {
                    "tenant": tenant_key,
                    "count": len(records),
                    "result": [record.to_public_dict() for record in records],
                    "env_fallback": registry.env_fallback_status(resolved_settings),
                }
            ),
            200,
        )

    @app.post("/ai/providers")
    @require_admin_token
    def provider_create():
        payload = request.get_json(silent=True) or {}
        api_key = str(payload.get("api_key") or "").strip()
        if not api_key:
            return _admin_error("API key is required", 400)
        try:
            parsed = registry.ProviderInput.parse(payload)
            record = registry.create_provider(resolved_settings, parsed, api_key)
        except registry.ProviderConfigError as exc:
            return _admin_error(str(exc), 400)
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(f"Could not save provider ({_error_class(exc)})", 503)
        return jsonify(record.to_public_dict()), 201

    @app.put("/ai/providers/<provider_id>")
    @require_admin_token
    def provider_update(provider_id: str):
        if not registry.is_provider_id(provider_id):
            return _admin_error("Provider not found", 404)
        payload = request.get_json(silent=True) or {}
        api_key = str(payload.get("api_key") or "").strip() or None
        try:
            parsed = registry.ProviderInput.parse(payload)
            record = registry.update_provider(
                resolved_settings, provider_id, parsed, api_key
            )
        except registry.ProviderConfigError as exc:
            return _admin_error(str(exc), 400)
        except LookupError:
            return _admin_error("Provider not found", 404)
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(f"Could not save provider ({_error_class(exc)})", 503)
        return jsonify(record.to_public_dict()), 200

    @app.delete("/ai/providers/<provider_id>")
    @require_admin_token
    def provider_delete(provider_id: str):
        if not registry.is_provider_id(provider_id):
            return _admin_error("Provider not found", 404)
        try:
            deleted = registry.delete_provider(resolved_settings, provider_id)
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(f"Could not delete provider ({_error_class(exc)})", 503)
        if not deleted:
            return _admin_error("Provider not found", 404)
        return jsonify({"deleted": True}), 200

    @app.post("/ai/providers/<provider_id>/test")
    @require_admin_token
    def provider_test(provider_id: str):
        if not registry.is_provider_id(provider_id):
            return _admin_error("Provider not found", 404)
        try:
            record = registry.get_provider(resolved_settings, provider_id)
            if record is None:
                return _admin_error("Provider not found", 404)
            api_key = registry.decrypt_api_key(resolved_settings, provider_id)
            provider = registry.build_provider(record, api_key)
        except LookupError:
            return _admin_error("Provider not found", 404)
        except (RuntimeError, registry.ProviderConfigError) as exc:
            return _admin_error(str(exc), 400)

        started = time.monotonic()
        try:
            response = provider.generate(
                ProviderRequest(
                    operation="provider.test",
                    messages=(Message("user", TEST_PROMPT),),
                    max_output_tokens=TEST_MAX_TOKENS,
                    temperature=0.0,
                )
            )
        except ProviderError as exc:
            duration_ms = round((time.monotonic() - started) * 1000)
            registry.record_test_result(
                resolved_settings,
                provider_id,
                status="failed",
                error=exc.code,
            )
            return (
                jsonify(
                    {
                        "status": "failed",
                        "error_code": exc.code,
                        "retryable": exc.retryable,
                        "duration_ms": duration_ms,
                    }
                ),
                200,
            )
        except Exception as exc:  # pragma: no cover - adapter failure path
            registry.record_test_result(
                resolved_settings,
                provider_id,
                status="failed",
                error=_error_class(exc),
            )
            return (
                jsonify(
                    {
                        "status": "failed",
                        "error_code": "adapter_error",
                        "retryable": False,
                    }
                ),
                200,
            )

        duration_ms = round((time.monotonic() - started) * 1000)
        registry.record_test_result(resolved_settings, provider_id, status="succeeded")
        return (
            jsonify(
                {
                    "status": "succeeded",
                    "model": response.model,
                    "duration_ms": duration_ms,
                    "finish_reason": response.finish_reason,
                }
            ),
            200,
        )

    return app


app = create_app()

