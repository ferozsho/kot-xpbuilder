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

import logging
import os
import time
import uuid
from collections.abc import Callable
from functools import wraps
from typing import Any

from flask import Flask, Response, jsonify, request

from xp_prompt import __version__, chat, conversations, jobs
from xp_prompt.config import Settings
from xp_prompt.health import readiness
from xp_prompt.providers import registry
from xp_prompt.providers.types import Message, ProviderError, ProviderRequest
from xp_prompt.security import constant_time_equals, internal_admin_token

ADMIN_TOKEN_HEADER = "X-XP-Prompt-Token"
TEST_PROMPT = "Reply with the single word OK."
TEST_MAX_TOKENS = 16
LOG_LEVEL_ENV = "XP_PROMPT_LOG_LEVEL"
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    """Attach one stdout handler to the ``xp_prompt`` logger.

    Gunicorn configures only its own loggers, so without this the structured
    attempt and probe lines never reach the container log. Idempotent, and the
    level is tunable with ``XP_PROMPT_LOG_LEVEL``.
    """
    package_logger = logging.getLogger("xp_prompt")
    level_name = (os.environ.get(LOG_LEVEL_ENV) or "INFO").strip().upper()
    package_logger.setLevel(getattr(logging, level_name, logging.INFO))
    if package_logger.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    package_logger.addHandler(handler)


def _admin_error(message: str, status: int) -> tuple[Response, int]:
    return jsonify({"message": message, "severity": "danger"}), status


def _error_class(exc: BaseException) -> str:
    return type(exc).__name__


def create_app(settings: Settings | None = None) -> Flask:
    """Create the XP Prompt API application."""
    _configure_logging()
    app = Flask(__name__)
    resolved_settings = settings or Settings.from_env()

    def _record_health(provider_id: str, error: ProviderError | None) -> None:
        """Best-effort passive health write; never fails the probe response."""
        try:
            if error is None:
                registry.record_attempt_success(resolved_settings, provider_id)
            else:
                registry.record_attempt_failure(
                    resolved_settings,
                    provider_id,
                    error_code=error.code,
                    retryable=error.retryable,
                )
        except Exception:  # pragma: no cover - bookkeeping only
            logger.warning("Could not persist provider health", exc_info=True)

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

    # --- Conversations (P2-F02) ----------------------------------------

    def _conversation_owner(payload: dict[str, Any] | None = None) -> int | None:
        """Resolve the owning Superset user id from the body or query string."""
        raw: Any = None
        if payload is not None:
            raw = payload.get("superset_user_id")
        if raw in (None, ""):
            raw = request.args.get("superset_user_id")
        if raw in (None, ""):
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    def _context_version(payload: dict[str, Any] | None) -> int | None | bool:
        """Parse the optional version check; ``False`` marks a bad value.

        ``False`` is used as the sentinel because ``int`` values are truthy and
        ``None``/0 are legitimate answers from this helper's perspective.
        """
        raw: Any = None
        if payload is not None:
            raw = payload.get("context_version")
        if raw in (None, ""):
            raw = request.args.get("context_version")
        if raw in (None, ""):
            return None
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return False
        return value if value > 0 else False

    @app.post("/ai/conversations")
    @require_admin_token
    def conversation_create():
        payload = request.get_json(silent=True) or {}
        owner = _conversation_owner(payload)
        if owner is None:
            return _admin_error("superset_user_id is required", 400)
        tenant_key = str(payload.get("tenant") or registry.DEFAULT_TENANT_KEY)
        try:
            record = conversations.create_conversation(
                resolved_settings,
                tenant_key=tenant_key,
                superset_user_id=owner,
                title=payload.get("title"),
            )
        except conversations.ConversationInputError as exc:
            return _admin_error(str(exc), 400)
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(f"Could not create conversation ({_error_class(exc)})", 503)
        return jsonify(record.to_public_dict()), 201

    @app.get("/ai/conversations")
    @require_admin_token
    def conversation_list():
        owner = _conversation_owner()
        if owner is None:
            return _admin_error("superset_user_id is required", 400)
        tenant_key = request.args.get("tenant") or registry.DEFAULT_TENANT_KEY
        try:
            limit = int(request.args.get("limit") or conversations.DEFAULT_LIST_LIMIT)
            include_archived = request.args.get("include_archived") == "true"
            records = conversations.list_conversations(
                resolved_settings,
                tenant_key=tenant_key,
                superset_user_id=owner,
                limit=limit,
                include_archived=include_archived,
            )
        except conversations.ConversationInputError as exc:
            return _admin_error(str(exc), 400)
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(f"Could not list conversations ({_error_class(exc)})", 503)
        return (
            jsonify(
                {
                    "tenant": tenant_key,
                    "count": len(records),
                    "result": [record.to_public_dict() for record in records],
                }
            ),
            200,
        )

    @app.get("/ai/conversations/<conversation_id>")
    @require_admin_token
    def conversation_detail(conversation_id: str):
        if not registry.is_provider_id(conversation_id):
            return _admin_error("Conversation not found", 404)
        owner = _conversation_owner()
        if owner is None:
            return _admin_error("superset_user_id is required", 400)
        tenant_key = request.args.get("tenant") or registry.DEFAULT_TENANT_KEY
        try:
            record = conversations.get_conversation(
                resolved_settings,
                conversation_id,
                tenant_key=tenant_key,
                superset_user_id=owner,
            )
            if record is None:
                return _admin_error("Conversation not found", 404)
            messages = conversations.list_messages(
                resolved_settings,
                conversation_id,
                tenant_key=tenant_key,
                superset_user_id=owner,
            )
        except conversations.ConversationInputError as exc:
            return _admin_error(str(exc), 400)
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(f"Could not load conversation ({_error_class(exc)})", 503)
        return (
            jsonify(
                {
                    "conversation": record.to_public_dict(),
                    "messages": [message.to_public_dict() for message in messages],
                }
            ),
            200,
        )

    @app.post("/ai/conversations/<conversation_id>/messages")
    @require_admin_token
    def conversation_message(conversation_id: str):
        if not registry.is_provider_id(conversation_id):
            return _admin_error("Conversation not found", 404)
        payload = request.get_json(silent=True) or {}
        owner = _conversation_owner(payload)
        if owner is None:
            return _admin_error("superset_user_id is required", 400)
        tenant_key = str(payload.get("tenant") or registry.DEFAULT_TENANT_KEY)
        content = str(payload.get("content") or "").strip()
        if not content:
            return _admin_error("content is required", 400)
        expected_version = _context_version(payload)
        if expected_version is False:  # sentinel: present but not an integer
            return _admin_error("context_version must be an integer", 400)
        try:
            turn = chat.run_turn(
                resolved_settings,
                conversation_id=conversation_id,
                tenant_key=tenant_key,
                superset_user_id=owner,
                content=content,
                expected_context_version=expected_version,
            )
        except conversations.ConversationNotFound:
            return _admin_error("Conversation not found", 404)
        except conversations.ContextVersionConflict:
            return _admin_error(
                "Conversation context changed; reload the conversation", 409
            )
        except conversations.ConversationInputError as exc:
            return _admin_error(str(exc), 400)
        except chat.ChatError as exc:
            status = 503 if exc.code == "no_provider" else 502
            return _admin_error(f"AI provider failed ({exc.code})", status)
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(f"Could not complete the turn ({_error_class(exc)})", 503)
        return (
            jsonify(
                {
                    "conversation_id": turn.conversation_id,
                    "correlation_id": turn.correlation_id,
                    "answer": turn.answer,
                    "provider": turn.provider,
                    "model": turn.model,
                    "intent": turn.intent,
                    "clarification": turn.clarification,
                    "target_intent": turn.target_intent,
                    "intent_reasons": list(turn.intent_reasons),
                    "usage": turn.usage,
                    "attempts": list(turn.attempts),
                }
            ),
            200,
        )

    @app.post("/ai/conversations/<conversation_id>/context/reset")
    @require_admin_token
    def conversation_context_reset(conversation_id: str):
        """Start a fresh context generation; the transcript itself is kept."""
        if not registry.is_provider_id(conversation_id):
            return _admin_error("Conversation not found", 404)
        payload = request.get_json(silent=True) or {}
        owner = _conversation_owner(payload)
        if owner is None:
            return _admin_error("superset_user_id is required", 400)
        tenant_key = str(payload.get("tenant") or registry.DEFAULT_TENANT_KEY)
        expected_version = _context_version(payload)
        if expected_version is False:
            return _admin_error("context_version must be an integer", 400)
        try:
            record = conversations.reset_context(
                resolved_settings,
                conversation_id,
                tenant_key=tenant_key,
                superset_user_id=owner,
                expected_version=expected_version,
            )
        except conversations.ConversationNotFound:
            return _admin_error("Conversation not found", 404)
        except conversations.ContextVersionConflict:
            return _admin_error(
                "Conversation context changed; reload the conversation", 409
            )
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(f"Could not reset context ({_error_class(exc)})", 503)
        return (
            jsonify(
                {
                    "conversation": record.to_public_dict(),
                    "context_version": record.context_version,
                }
            ),
            200,
        )

    @app.post("/ai/conversations/<conversation_id>/archive")
    @require_admin_token
    def conversation_archive(conversation_id: str):
        if not registry.is_provider_id(conversation_id):
            return _admin_error("Conversation not found", 404)
        payload = request.get_json(silent=True) or {}
        owner = _conversation_owner(payload)
        if owner is None:
            return _admin_error("superset_user_id is required", 400)
        tenant_key = str(payload.get("tenant") or registry.DEFAULT_TENANT_KEY)
        try:
            archived = conversations.archive_conversation(
                resolved_settings,
                conversation_id,
                tenant_key=tenant_key,
                superset_user_id=owner,
            )
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(f"Could not archive conversation ({_error_class(exc)})", 503)
        if not archived:
            return _admin_error("Conversation not found", 404)
        return jsonify({"archived": True}), 200

    # --- Jobs (P2-F05) -------------------------------------------------

    def _enqueue_job(job_id: str) -> None:
        """Hand a queued job to Celery; the database remains the queue state."""
        try:
            from xp_prompt.tasks import run_job

            run_job.delay(job_id)
        except Exception:  # pragma: no cover - broker outage is recoverable
            logger.warning(
                "Could not enqueue job %s; the recovery sweep will pick it up",
                job_id,
                exc_info=True,
            )

    @app.post("/ai/jobs")
    @require_admin_token
    def job_create():
        payload = request.get_json(silent=True) or {}
        owner = _conversation_owner(payload)
        if owner is None:
            return _admin_error("superset_user_id is required", 400)
        conversation_id = str(payload.get("conversation_id") or "").strip()
        if not registry.is_provider_id(conversation_id):
            return _admin_error("conversation_id must be a UUID", 400)
        tenant_key = str(payload.get("tenant") or registry.DEFAULT_TENANT_KEY)
        request_payload = payload.get("request")
        steps = payload.get("steps") or ()
        if not isinstance(steps, list | tuple):
            return _admin_error("steps must be a list", 400)
        job_context_version = _context_version(payload)
        if job_context_version is False:
            return _admin_error("context_version must be an integer", 400)
        try:
            record = jobs.create_job(
                resolved_settings,
                conversation_id=conversation_id,
                tenant_key=tenant_key,
                superset_user_id=owner,
                job_type=str(payload.get("job_type") or ""),
                idempotency_key=str(payload.get("idempotency_key") or ""),
                request=request_payload if isinstance(request_payload, dict) else {},
                steps=tuple(str(step) for step in steps),
                context_version=job_context_version or 1,
                max_attempts=payload.get("max_attempts") or jobs.DEFAULT_MAX_ATTEMPTS,
            )
        except jobs.JobIdempotencyConflict:
            return _admin_error(
                "idempotency_key was already used for a different request", 409
            )
        except jobs.JobInputError as exc:
            return _admin_error(str(exc), 400)
        except jobs.JobNotFound:
            return _admin_error("Conversation not found", 404)
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(f"Could not create job ({_error_class(exc)})", 503)
        if not record.replayed and record.status == jobs.QUEUED:
            _enqueue_job(record.id)
        return jsonify(record.to_public_dict()), 200 if record.replayed else 201

    @app.get("/ai/jobs")
    @require_admin_token
    def job_list():
        owner = _conversation_owner()
        if owner is None:
            return _admin_error("superset_user_id is required", 400)
        conversation_id = str(request.args.get("conversation_id") or "").strip()
        if not registry.is_provider_id(conversation_id):
            return _admin_error("conversation_id must be a UUID", 400)
        tenant_key = request.args.get("tenant") or registry.DEFAULT_TENANT_KEY
        try:
            records = jobs.list_jobs(
                resolved_settings,
                conversation_id,
                tenant_key=tenant_key,
                superset_user_id=owner,
            )
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(f"Could not list jobs ({_error_class(exc)})", 503)
        return (
            jsonify({"count": len(records), "result": [r.to_public_dict() for r in records]}),
            200,
        )

    @app.get("/ai/jobs/<job_id>")
    @require_admin_token
    def job_detail(job_id: str):
        if not jobs.is_job_id(job_id):
            return _admin_error("Job not found", 404)
        owner = _conversation_owner()
        if owner is None:
            return _admin_error("superset_user_id is required", 400)
        tenant_key = request.args.get("tenant") or registry.DEFAULT_TENANT_KEY
        try:
            record = jobs.get_job(
                resolved_settings,
                job_id,
                tenant_key=tenant_key,
                superset_user_id=owner,
            )
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(f"Could not load job ({_error_class(exc)})", 503)
        if record is None:
            return _admin_error("Job not found", 404)
        return jsonify(record.to_public_dict()), 200

    @app.post("/ai/jobs/<job_id>/cancel")
    @require_admin_token
    def job_cancel(job_id: str):
        if not jobs.is_job_id(job_id):
            return _admin_error("Job not found", 404)
        payload = request.get_json(silent=True) or {}
        owner = _conversation_owner(payload)
        if owner is None:
            return _admin_error("superset_user_id is required", 400)
        tenant_key = str(payload.get("tenant") or registry.DEFAULT_TENANT_KEY)
        try:
            record = jobs.request_cancel(
                resolved_settings,
                job_id,
                tenant_key=tenant_key,
                superset_user_id=owner,
            )
        except jobs.JobNotFound:
            return _admin_error("Job not found", 404)
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(f"Could not cancel job ({_error_class(exc)})", 503)
        return jsonify(record.to_public_dict()), 200

    @app.post("/ai/jobs/<job_id>/retry")
    @require_admin_token
    def job_retry(job_id: str):
        if not jobs.is_job_id(job_id):
            return _admin_error("Job not found", 404)
        payload = request.get_json(silent=True) or {}
        owner = _conversation_owner(payload)
        if owner is None:
            return _admin_error("superset_user_id is required", 400)
        tenant_key = str(payload.get("tenant") or registry.DEFAULT_TENANT_KEY)
        try:
            record = jobs.retry_job(
                resolved_settings,
                job_id,
                tenant_key=tenant_key,
                superset_user_id=owner,
            )
        except jobs.JobNotFound:
            return _admin_error("Job not found", 404)
        except jobs.JobStateError as exc:
            return _admin_error(str(exc), 409)
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(f"Could not retry job ({_error_class(exc)})", 503)
        _enqueue_job(record.id)
        return jsonify(record.to_public_dict()), 200

    @app.post("/ai/jobs/<job_id>/confirm")
    @require_admin_token
    def job_confirm(job_id: str):
        """Approve a mutating job; nothing runs until this is called (P2-F08)."""
        if not jobs.is_job_id(job_id):
            return _admin_error("Job not found", 404)
        payload = request.get_json(silent=True) or {}
        owner = _conversation_owner(payload)
        if owner is None:
            return _admin_error("superset_user_id is required", 400)
        tenant_key = str(payload.get("tenant") or registry.DEFAULT_TENANT_KEY)
        try:
            record = jobs.confirm_job(
                resolved_settings,
                job_id,
                tenant_key=tenant_key,
                superset_user_id=owner,
            )
        except jobs.JobNotFound:
            return _admin_error("Job not found", 404)
        except jobs.JobStateError as exc:
            return _admin_error(str(exc), 409)
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(f"Could not confirm job ({_error_class(exc)})", 503)
        _enqueue_job(record.id)
        return jsonify(record.to_public_dict()), 200

    @app.post("/ai/jobs/<job_id>/reject")
    @require_admin_token
    def job_reject(job_id: str):
        """Refuse a mutating job; it ends cancelled without running."""
        if not jobs.is_job_id(job_id):
            return _admin_error("Job not found", 404)
        payload = request.get_json(silent=True) or {}
        owner = _conversation_owner(payload)
        if owner is None:
            return _admin_error("superset_user_id is required", 400)
        tenant_key = str(payload.get("tenant") or registry.DEFAULT_TENANT_KEY)
        try:
            record = jobs.reject_job(
                resolved_settings,
                job_id,
                tenant_key=tenant_key,
                superset_user_id=owner,
            )
        except jobs.JobNotFound:
            return _admin_error("Job not found", 404)
        except jobs.JobStateError as exc:
            return _admin_error(str(exc), 409)
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(f"Could not reject job ({_error_class(exc)})", 503)
        return jsonify(record.to_public_dict()), 200

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
        correlation_id = uuid.uuid4().hex
        try:
            response = provider.generate(
                ProviderRequest(
                    operation="provider.test",
                    messages=(Message("user", TEST_PROMPT),),
                    max_output_tokens=TEST_MAX_TOKENS,
                    temperature=0.0,
                    correlation_id=correlation_id,
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
            _record_health(provider_id, exc)
            logger.info(
                "provider_test correlation_id=%s provider=%s outcome=failed "
                "error_code=%s duration_ms=%d",
                correlation_id,
                record.name,
                exc.code,
                duration_ms,
            )
            return (
                jsonify(
                    {
                        "status": "failed",
                        "error_code": exc.code,
                        "retryable": exc.retryable,
                        "duration_ms": duration_ms,
                        "correlation_id": correlation_id,
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
                        "correlation_id": correlation_id,
                    }
                ),
                200,
            )

        duration_ms = round((time.monotonic() - started) * 1000)
        registry.record_test_result(resolved_settings, provider_id, status="succeeded")
        _record_health(provider_id, None)
        logger.info(
            "provider_test correlation_id=%s provider=%s outcome=succeeded "
            "model=%s duration_ms=%d",
            correlation_id,
            record.name,
            response.model,
            duration_ms,
        )
        return (
            jsonify(
                {
                    "status": "succeeded",
                    "model": response.model,
                    "duration_ms": duration_ms,
                    "finish_reason": response.finish_reason,
                    "correlation_id": correlation_id,
                }
            ),
            200,
        )

    return app


app = create_app()

