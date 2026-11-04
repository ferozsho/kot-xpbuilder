"""HTTP surface for the KOT5 XP Prompt API process.

Three route groups live here:

* ``/health/*`` — unauthenticated liveness/readiness probes.
* ``/ai/providers`` — admin-managed provider configuration. The XPBuilder React
  settings page never calls these directly: the Superset backend proxies them
  after enforcing Flask-AppBuilder authorization and signs each request with the
  internal service token derived from the root secret.
* ``/ai/providers/<id>/test`` — redacted provider connection test.

The chat workspace (``/ai/conversations``, ``/ai/jobs``, ``/ai/artifacts``) is
reached the same way: through the Superset proxy, which injects the caller's
identity from the FAB session.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from functools import wraps
from typing import Any

from flask import Flask, Response, jsonify, request

from xp_prompt import (
    __version__,
    artifacts,
    audit,
    billing,
    budgets,
    chart_catalog,
    chart_draft,
    chart_profile,
    chart_ranking,
    chart_rules,
    chat,
    conversations,
    cost_guard,
    dashboard_layout,
    dashboard_plan,
    dashboard_preview,
    dashboard_publish,
    dataset_draft,
    dataset_quality,
    dataset_spec,
    failures,
    jobs,
    outbox,
    patterns,
    repair,
    responses,
    result_validation,
    schema_slice,
    settings_registry,
    sql_candidate,
    sql_execution,
    sql_guard,
    usage,
)
from xp_prompt.config import Settings
from xp_prompt.health import readiness
from xp_prompt.providers import registry
from xp_prompt.providers.types import (
    AttemptSummary,
    Message,
    ProviderError,
    ProviderRequest,
)
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


def _is_uuid(value: Any) -> bool:
    """Route ids are canonical UUIDs; anything else is a 404, not a query."""
    try:
        return str(uuid.UUID(str(value))) == str(value)
    except (TypeError, ValueError):
        return False


def _optional_uuid(value: Any) -> str | None:
    """Link a record to a conversation or job only when the id is well-formed."""
    text = str(value or "").strip()
    return text if _is_uuid(text) else None


def _optional_int(value: Any) -> int | None:
    """An integer field that is meaningful when absent, e.g. a discount rate."""
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


#: Chat failures the platform decided on, as opposed to a provider that broke.
_CHAT_REFUSALS = frozenset({"budget_exhausted", "feature_disabled"})


def _chat_error_status(code: str) -> int:
    """A chat failure's status: a refusal is not the same as an outage."""
    return {
        "no_provider": 503,
        "budget_exhausted": 429,
        "feature_disabled": 403,
    }.get(code, 502)


def _parse_timestamp(value: Any) -> Any:
    """Accept an ISO date or datetime; an unparseable value means "not filtered".

    Usage queries are read-only reporting, so a bad ``since`` should widen the
    window visibly rather than answer 400 and leave the admin staring at nothing.
    """
    text = str(value or "").strip()
    if not text:
        return None
    candidate = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(f"{candidate}T00:00:00")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


#: P4-F04 refusals -> HTTP status. A blocked statement is a policy outcome (422),
#: an exhausted provider is availability (503), a bad answer is upstream (502).
_SQL_CANDIDATE_STATUS = {
    "empty_question": 400,
    "question_too_long": 400,
    "unsupported_dialect": 400,
    "dialect_ambiguous": 400,
    "no_schema": 409,
    "sql_blocked": 422,
    "semantic_invalid": 422,
    "ambiguous_question": 422,
    "invalid_response": 502,
    "provider_error": 503,
    "no_provider": 503,
    "feature_disabled": 403,
}


def _sql_candidate_status(exc: BaseException) -> int:
    return _SQL_CANDIDATE_STATUS.get(getattr(exc, "code", ""), 503)


def _sql_candidate_message(exc: BaseException) -> str:
    """The refusal, plus the guard's or provider's code when there is one."""
    code = str(getattr(exc, "code", "error"))
    detail = (
        getattr(exc, "guard_code", None)
        or getattr(exc, "provider_code", None)
    )
    if detail:
        return f"SQL candidate rejected ({code}: {detail})"
    # A semantic refusal puts its explanation in the message (which column, which
    # dataset), and that is the part a user can act on.
    reason = str(exc).strip()
    if reason and reason != code:
        return f"SQL candidate rejected ({code}): {reason}"
    return f"SQL candidate rejected ({code})"


def _draft_conflict_message(exc: dataset_draft.DraftConflict) -> str:
    """A stale writer is told which version it lost to, not just "conflict"."""
    return (
        f"This draft was edited elsewhere (you had version {exc.expected}, "
        f"it is now at {exc.actual}). Reload it before saving."
    )


def create_app(settings: Settings | None = None) -> Flask:
    """Create the XP Prompt API application."""
    _configure_logging()
    app = Flask(__name__)
    resolved_settings = settings or Settings.from_env()

    def _cost_budget() -> cost_guard.Budget:
        """The deployment's cost budget (P4-F08), from validated settings."""
        return cost_guard.Budget(
            max_rows=resolved_settings.max_estimated_rows,
            max_cost=resolved_settings.max_estimated_cost,
            max_plan_nodes=resolved_settings.max_plan_nodes,
            max_runtime_ms=resolved_settings.max_runtime_ms,
            require_plan=resolved_settings.require_plan,
        )

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

    def _record_connection_test(
        name: str,
        model: str,
        correlation_id: str,
        duration_ms: int,
        *,
        response: Any = None,
        error: ProviderError | None = None,
    ) -> None:
        """Put the settings-page probe in the usage ledger too (P9-F01).

        The probe bypasses the router, so without this it would be the one AI call
        that spends tokens invisibly. It is best effort: a ledger write must never
        turn a working connection into a failed test.
        """
        try:
            usage.record(
                resolved_settings,
                usage.Context(module="provider_test", feature="connection_test"),
                AttemptSummary(
                    provider=name,
                    model=getattr(response, "model", None) or model,
                    outcome="failed" if error is not None else "succeeded",
                    error_code=error.code if error is not None else None,
                    attempt=1,
                    correlation_id=correlation_id,
                    operation="provider.test",
                    usage=getattr(response, "usage", None),
                    duration_ms=duration_ms,
                    request_id=getattr(response, "request_id", None),
                    error_detail=str(error) if error is not None else "",
                ),
            )
        except Exception:  # pragma: no cover - bookkeeping only
            logger.warning("Could not record provider test usage", exc_info=True)

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
            # A budget refusal or a switched-off feature is a deliberate stop, not
            # an upstream fault, so it answers with the module's own sentence and
            # its own status instead of a generic 502 (P9-F09, P10-F08).
            status = _chat_error_status(exc.code)
            message = (
                str(exc)
                if exc.code in _CHAT_REFUSALS
                else f"AI provider failed ({exc.code})"
            )
            return _admin_error(message, status)
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

    # --- Artifacts (P2-F07) ---------------------------------------------

    @app.post("/ai/artifacts")
    @require_admin_token
    def artifact_create():
        """Record a draft proposal. Producers (Phases 6–8) and tests call this."""
        payload = request.get_json(silent=True) or {}
        owner = _conversation_owner(payload)
        if owner is None:
            return _admin_error("superset_user_id is required", 400)
        conversation_id = str(payload.get("conversation_id") or "").strip()
        if not registry.is_provider_id(conversation_id):
            return _admin_error("conversation_id must be a UUID", 400)
        tenant_key = str(payload.get("tenant") or registry.DEFAULT_TENANT_KEY)
        body = payload.get("payload")
        try:
            record = artifacts.create_artifact(
                resolved_settings,
                conversation_id=conversation_id,
                tenant_key=tenant_key,
                superset_user_id=owner,
                artifact_type=str(payload.get("artifact_type") or ""),
                title=str(payload.get("title") or ""),
                payload=body if isinstance(body, dict) else {},
                job_id=str(payload.get("job_id") or "") or None,
            )
        except artifacts.ArtifactInputError as exc:
            return _admin_error(str(exc), 400)
        except artifacts.ArtifactNotFound:
            return _admin_error("Conversation not found", 404)
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(f"Could not create artifact ({_error_class(exc)})", 503)
        return jsonify(record.to_public_dict()), 201

    @app.get("/ai/artifacts/<artifact_id>")
    @require_admin_token
    def artifact_detail(artifact_id: str):
        if not artifacts.is_artifact_id(artifact_id):
            return _admin_error("Artifact not found", 404)
        owner = _conversation_owner()
        if owner is None:
            return _admin_error("superset_user_id is required", 400)
        tenant_key = request.args.get("tenant") or registry.DEFAULT_TENANT_KEY
        try:
            record = artifacts.get_artifact(
                resolved_settings,
                artifact_id,
                tenant_key=tenant_key,
                superset_user_id=owner,
            )
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(f"Could not load artifact ({_error_class(exc)})", 503)
        if record is None:
            return _admin_error("Artifact not found", 404)
        return jsonify(record.to_public_dict()), 200

    @app.post("/ai/artifacts/<artifact_id>/<action>")
    @require_admin_token
    def artifact_action(artifact_id: str, action: str):
        """confirm / discard / publish — one lifecycle, no per-type endpoints."""
        if not artifacts.is_artifact_id(artifact_id):
            return _admin_error("Artifact not found", 404)
        if action not in {"confirm", "discard", "publish"}:
            return _admin_error("Unsupported artifact action", 404)
        payload = request.get_json(silent=True) or {}
        owner = _conversation_owner(payload)
        if owner is None:
            return _admin_error("superset_user_id is required", 400)
        tenant_key = str(payload.get("tenant") or registry.DEFAULT_TENANT_KEY)
        try:
            if action == "confirm":
                record = artifacts.confirm_artifact(
                    resolved_settings,
                    artifact_id,
                    tenant_key=tenant_key,
                    superset_user_id=owner,
                )
            elif action == "discard":
                record = artifacts.discard_artifact(
                    resolved_settings,
                    artifact_id,
                    tenant_key=tenant_key,
                    superset_user_id=owner,
                )
            else:
                record = artifacts.publish_artifact(
                    resolved_settings,
                    artifact_id,
                    tenant_key=tenant_key,
                    superset_user_id=owner,
                    object_type=str(payload.get("object_type") or ""),
                    object_id=str(payload.get("object_id") or ""),
                )
        except artifacts.ArtifactNotFound:
            return _admin_error("Artifact not found", 404)
        except artifacts.ArtifactStateError as exc:
            return _admin_error(str(exc), 409)
        except artifacts.ArtifactInputError as exc:
            return _admin_error(str(exc), 400)
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(f"Could not update artifact ({_error_class(exc)})", 503)
        return jsonify(record.to_public_dict()), 200

    @app.get("/ai/conversations/<conversation_id>/artifacts")
    @require_admin_token
    def conversation_artifacts(conversation_id: str):
        if not registry.is_provider_id(conversation_id):
            return _admin_error("Conversation not found", 404)
        owner = _conversation_owner()
        if owner is None:
            return _admin_error("superset_user_id is required", 400)
        tenant_key = request.args.get("tenant") or registry.DEFAULT_TENANT_KEY
        try:
            records = artifacts.list_artifacts(
                resolved_settings,
                conversation_id,
                tenant_key=tenant_key,
                superset_user_id=owner,
            )
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(f"Could not list artifacts ({_error_class(exc)})", 503)
        return (
            jsonify(
                {"count": len(records), "result": [r.to_public_dict() for r in records]}
            ),
            200,
        )

    # --- Authorized schema slice (P4-F02) -------------------------------

    @app.post("/ai/schema")
    @require_admin_token
    def schema_slice_normalize():
        """Validate the slice Superset built for this principal.

        Superset resolves the authorization; this endpoint only bounds, validates
        and fingerprints it. The prompt text is returned only when the caller asks
        for it, because an internal consumer needs it and a browser does not.
        """
        payload = request.get_json(silent=True) or {}
        if _conversation_owner(payload) is None:
            return _admin_error("superset_user_id is required", 400)
        slice_ = schema_slice.SchemaSlice.from_payload(payload)
        return (
            jsonify(
                slice_.to_public_dict(include_prompt=bool(payload.get("include_prompt")))
            ),
            200,
        )

    # --- Candidate SQL (P4-F04) -----------------------------------------

    @app.post("/ai/sql/candidate")
    @require_admin_token
    def sql_candidate_generate():
        """Generate one candidate against the authorized slice and guard it.

        The slice arrives with the request because Superset resolved it for this
        principal; the SQL never leaves unvalidated.
        """
        payload = request.get_json(silent=True) or {}
        if _conversation_owner(payload) is None:
            return _admin_error("superset_user_id is required", 400)
        slice_ = schema_slice.SchemaSlice.from_payload(payload)
        database = str(payload.get("database") or "").strip() or None
        dialect = str(payload.get("dialect") or "").strip() or None
        try:
            candidate = sql_candidate.generate(
                resolved_settings,
                question=str(payload.get("question") or ""),
                schema=slice_,
                tenant_key=str(
                    payload.get("tenant") or registry.DEFAULT_TENANT_KEY
                ),
                database=database,
                dialect=dialect,
                max_rows=payload.get("max_rows") or sql_guard.MAX_ROWS,
                # Explicit regeneration: honoured even when a cached statement
                # exists, and reported as ``cache: "bypass"`` (P4-F11).
                bypass_cache=bool(payload.get("bypass_cache")),
                # Who spent the tokens, so the usage ledger can attribute a call
                # rather than merely count it (P9-F02).
                superset_user_id=_conversation_owner(payload),
                conversation_id=payload.get("conversation_id") or None,
            )
        except sql_candidate.SqlCandidateError as exc:
            return (
                jsonify(
                    {
                        "message": _sql_candidate_message(exc),
                        "severity": "danger",
                        # Machine-readable, so the proxy can audit the refusal
                        # without parsing the message.
                        "code": exc.code,
                        "guard_code": getattr(exc, "guard_code", None),
                    }
                ),
                _sql_candidate_status(exc),
            )
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(f"Could not generate SQL ({_error_class(exc)})", 503)
        response = candidate.to_public_dict()
        response["guard_code"] = None
        return jsonify(response), 200

    @app.post("/ai/sql/validate")
    @require_admin_token
    def sql_validate():
        """Re-run the guard on a statement that is about to be executed (P4-F09).

        The proxy calls this immediately before executing, so a SQL string cannot
        reach a database on the strength of an earlier check — or of the client.
        """
        payload = request.get_json(silent=True) or {}
        if _conversation_owner(payload) is None:
            return _admin_error("superset_user_id is required", 400)
        slice_ = schema_slice.SchemaSlice.from_payload(payload)
        database = str(payload.get("database") or "").strip() or None
        dialect = str(payload.get("dialect") or "").strip().lower() or None
        try:
            result = sql_execution.validate(
                str(payload.get("sql") or ""),
                schema=slice_,
                database=database,
                dialect=dialect,
                max_rows=payload.get("max_rows") or sql_guard.MAX_ROWS,
            )
        except sql_guard.SqlPolicyError as exc:
            status = 400 if exc.code in {"dialect_ambiguous", "unsupported_dialect"} else 422
            detail = str(exc).strip()
            suffix = f": {detail}" if detail else ""
            return _admin_error(
                f"SQL candidate rejected (sql_blocked: {exc.code}){suffix}", status
            )
        return jsonify(result), 200

    @app.post("/ai/sql/plan")
    @require_admin_token
    def sql_plan_verdict():
        """Decide whether a statement's cost fits the budget (P4-F08).

        Superset measures (it is the only component with a connection) and sends a
        normalized estimate; this endpoint owns the budget, so a caller cannot make
        its own query cheaper by reporting a friendlier number.
        """
        payload = request.get_json(silent=True) or {}
        if _conversation_owner(payload) is None:
            return _admin_error("superset_user_id is required", 400)
        estimate = cost_guard.Estimate.from_payload(payload.get("estimate"))
        verdict = cost_guard.evaluate(estimate, _cost_budget())
        logger.info(
            "sql_plan outcome=%s code=%s source=%s rows=%s nodes=%s",
            "allowed" if verdict.allowed else "refused",
            verdict.code,
            verdict.estimate.source,
            verdict.estimate.rows,
            verdict.estimate.plan_nodes,
        )
        return jsonify(verdict.to_public_dict()), 200

    @app.post("/ai/sql/failure")
    @require_admin_token
    def sql_failure_classify():
        """Classify a failure and say whether it may be repaired (P5-F01/F06).

        The proxy measures what the database said; this endpoint owns the taxonomy,
        the fingerprint and the budget, so the same failure is classified the same
        way whoever reports it, and a policy refusal can never be "healed" around.
        """
        payload = request.get_json(silent=True) or {}
        owner = _conversation_owner(payload)
        if owner is None:
            return _admin_error("superset_user_id is required", 400)
        event = failures.classify(
            stage=str(payload.get("stage") or "execute"),
            kind_hint=str(payload.get("kind") or ""),
            error_class=str(payload.get("error_class") or ""),
            sqlstate=str(payload.get("sqlstate") or ""),
            errno=str(payload.get("errno") or ""),
            message=str(payload.get("message") or ""),
        )
        state = failures.RepairState(
            attempts=int(payload.get("attempts") or 0),
            elapsed_ms=int(payload.get("elapsed_ms") or 0),
            tokens=int(payload.get("tokens") or 0),
        )
        budget = failures.budget_from_settings(resolved_settings)
        allowed, reason = failures.can_repair(event, state, budget)
        tenant_key = str(payload.get("tenant") or registry.DEFAULT_TENANT_KEY)
        conversation_id = str(payload.get("conversation_id") or "").strip() or None
        failures.record_attempt(
            resolved_settings,
            tenant_key=tenant_key,
            fingerprint_value=event.fingerprint,
            kind=event.kind,
            stage=event.stage,
            outcome="failed",
            conversation_id=conversation_id,
        )
        return (
            jsonify(
                {
                    "failure": event.to_public_dict(),
                    "repair": {
                        "allowed": allowed,
                        "reason": reason,
                        "budget": budget.to_public_dict(),
                        "state": state.to_public_dict(),
                    },
                }
            ),
            200,
        )

    # --- Repair and validated knowledge (P5-F02…F10) ---------------------

    @app.post("/ai/sql/repair")
    @require_admin_token
    def sql_repair_propose():
        """Propose what to try next for a failure — or refuse to try anything.

        The caller reports the failure and what it has already spent; this endpoint
        owns the taxonomy, the budget, the pattern library and the guard. A proposal
        is a *validated* statement: it has passed the same guard and semantic check as
        a first attempt, and the caller must still authorize, cost-check, execute and
        validate its result before presenting it (P5-F05).
        """
        payload = request.get_json(silent=True) or {}
        if _conversation_owner(payload) is None:
            return _admin_error("superset_user_id is required", 400)
        slice_ = schema_slice.SchemaSlice.from_payload(payload)
        proposal = repair.propose(
            resolved_settings,
            question=str(payload.get("question") or ""),
            schema=slice_,
            tenant_key=str(payload.get("tenant") or registry.DEFAULT_TENANT_KEY),
            stage=str(payload.get("stage") or "execute"),
            kind_hint=str(payload.get("kind") or ""),
            error_class=str(payload.get("error_class") or ""),
            sqlstate=str(payload.get("sqlstate") or ""),
            errno=str(payload.get("errno") or ""),
            message=str(payload.get("message") or ""),
            failed_sql=str(payload.get("sql") or ""),
            database=str(payload.get("database") or "").strip() or None,
            dialect=str(payload.get("dialect") or "").strip() or None,
            max_rows=payload.get("max_rows") or sql_guard.MAX_ROWS,
            attempts=int(payload.get("attempts") or 0),
            elapsed_ms=int(payload.get("elapsed_ms") or 0),
            tokens=int(payload.get("tokens") or 0),
            intent=str(payload.get("intent") or "query"),
            conversation_id=str(payload.get("conversation_id") or "").strip() or None,
        )
        return jsonify(proposal.to_public_dict()), 200

    @app.post("/ai/sql/repair/outcome")
    @require_admin_token
    def sql_repair_outcome():
        """Record how a repair ended, and let the knowledge catch up (P5-F07/F08).

        A success is evidence; a failure is both evidence and a suppression, so the
        same fix is not offered for the same failure twice.
        """
        payload = request.get_json(silent=True) or {}
        if _conversation_owner(payload) is None:
            return _admin_error("superset_user_id is required", 400)
        failure = payload.get("failure")
        if not isinstance(failure, dict) or not failure.get("fingerprint"):
            return _admin_error("failure is required", 400)
        slice_ = None
        if payload.get("datasets") is not None and payload.get("sql"):
            slice_ = schema_slice.SchemaSlice.from_payload(payload)
        result = repair.record_outcome(
            resolved_settings,
            tenant_key=str(payload.get("tenant") or registry.DEFAULT_TENANT_KEY),
            failure=failure,
            succeeded=bool(payload.get("succeeded")),
            fix_fingerprint_value=str(payload.get("fix_fingerprint") or ""),
            pattern_id=str(payload.get("pattern_id") or ""),
            sql=str(payload.get("sql") or ""),
            schema=slice_,
            dialect=str(payload.get("dialect") or ""),
            conversation_id=str(payload.get("conversation_id") or "").strip() or None,
            threshold=resolved_settings.pattern_evidence_threshold,
        )
        return jsonify(result), 200

    @app.get("/ai/sql/patterns")
    @require_admin_token
    def sql_pattern_list():
        """Operator view of the stored knowledge, with no statement in it (P5-F10)."""
        tenant_key = str(request.args.get("tenant") or registry.DEFAULT_TENANT_KEY)
        found = patterns.list_patterns(
            resolved_settings,
            tenant_key=tenant_key,
            state=str(request.args.get("state") or ""),
            failure_fingerprint=str(request.args.get("failure") or ""),
            limit=int(request.args.get("limit") or patterns.DEFAULT_LIST_LIMIT),
        )
        return (
            jsonify(
                {
                    "tenant": tenant_key,
                    "count": len(found),
                    "result": [pattern.to_public_dict() for pattern in found],
                }
            ),
            200,
        )

    @app.get("/ai/sql/repair/metrics")
    @require_admin_token
    def sql_repair_metrics():
        """Counts, states and success rates — the numbers, not the content (P5-F10)."""
        tenant_key = str(request.args.get("tenant") or registry.DEFAULT_TENANT_KEY)
        metrics = patterns.metrics(resolved_settings, tenant_key=tenant_key)
        thresholds = {
            "evidence": resolved_settings.pattern_evidence_threshold,
            "auto_promote": resolved_settings.pattern_auto_promote,
            "validator_version": patterns.VALIDATOR_VERSION,
            "policy_version": patterns.POLICY_VERSION,
        }
        return (
            jsonify(
                {
                    "tenant": tenant_key,
                    "metrics": metrics,
                    "promotion": thresholds,
                    "budget": failures.budget_from_settings(
                        resolved_settings
                    ).to_public_dict(),
                }
            ),
            200,
        )

    @app.post("/ai/sql/patterns/<pattern_id>/<action>")
    @require_admin_token
    def sql_pattern_action(pattern_id: str, action: str):
        """Promote, deprecate, quarantine or restore one pattern (P5-F08/F09).

        ``promote`` is the only action that grants reuse, and without ``force`` it
        enforces the evidence threshold; ``quarantine`` stops reuse immediately.
        """
        payload = request.get_json(silent=True) or {}
        if _conversation_owner(payload) is None:
            return _admin_error("superset_user_id is required", 400)
        tenant_key = str(payload.get("tenant") or registry.DEFAULT_TENANT_KEY)
        try:
            if action == "promote":
                result = patterns.promote(
                    resolved_settings,
                    tenant_key=tenant_key,
                    pattern_id=pattern_id,
                    threshold=resolved_settings.pattern_evidence_threshold,
                    force=bool(payload.get("force")),
                )
            else:
                result = patterns.set_state(
                    resolved_settings,
                    tenant_key=tenant_key,
                    pattern_id=pattern_id,
                    action=action,
                    reason=str(payload.get("reason") or ""),
                )
        except patterns.PatternError as exc:
            status = 404 if exc.code == "not_found" else 422
            return _admin_error(f"Pattern refused ({exc.code}): {exc}", status)
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(f"Could not update the pattern ({_error_class(exc)})", 503)
        return jsonify(result), 200

    @app.post("/ai/sql/patterns/revalidate")
    @require_admin_token
    def sql_pattern_revalidate():
        """Send patterns proven under an older validator or policy back to validation."""
        payload = request.get_json(silent=True) or {}
        if _conversation_owner(payload) is None:
            return _admin_error("superset_user_id is required", 400)
        tenant_key = str(payload.get("tenant") or registry.DEFAULT_TENANT_KEY)
        changed = patterns.revalidate_stale(resolved_settings, tenant_key=tenant_key)
        return jsonify({"revalidated": changed}), 200

    @app.post("/ai/sql/result")
    @require_admin_token
    def sql_result_validate():
        """Decide whether a result is usable and build its preview blocks (P4-F10).

        The proxy executes SQL in Superset; this endpoint is the only place that
        decides what a caller may see of the result and how it renders.
        """
        payload = request.get_json(silent=True) or {}
        if _conversation_owner(payload) is None:
            return _admin_error("superset_user_id is required", 400)
        try:
            verdict = result_validation.validate_result(
                payload.get("columns"),
                payload.get("rows"),
                row_count=payload.get("row_count")
                if isinstance(payload.get("row_count"), int)
                else None,
                truncated=bool(payload.get("truncated")),
                intent=str(payload.get("intent") or "") or None,
                max_rows=payload.get("max_rows") or result_validation.MAX_PREVIEW_ROWS,
            )
        except result_validation.ResultValidationError as exc:
            return _admin_error(f"Result rejected ({exc.code})", 422)
        return jsonify(verdict.to_public_dict()), 200

    @app.post("/ai/conversations/<conversation_id>/blocks")
    @require_admin_token
    def conversation_append_blocks(conversation_id: str):
        """Store a completed SQL turn (P4-F09/F10 join).

        Superset executes and therefore owns the last two steps, but the content
        and the blocks are still authored by this service (``sql_candidate`` and
        ``result_validation``); this endpoint only *stores* them, re-validating the
        blocks so a caller cannot inject an unnormalized shape into a transcript.
        """
        if not registry.is_provider_id(conversation_id):
            return _admin_error("Conversation not found", 404)
        payload = request.get_json(silent=True) or {}
        owner = _conversation_owner(payload)
        if owner is None:
            return _admin_error("superset_user_id is required", 400)
        tenant_key = str(payload.get("tenant") or registry.DEFAULT_TENANT_KEY)
        kind = str(payload.get("kind") or "answer")
        if kind not in {"answer", "clarification", "error"}:
            return _admin_error("Unsupported message kind", 400)
        blocks = responses.normalize_blocks(payload.get("blocks"))
        if not blocks:
            return _admin_error("blocks are required", 400)
        content = str(payload.get("content") or "").strip()
        question = str(payload.get("question") or "").strip()
        try:
            if question:
                conversations.append_message(
                    resolved_settings,
                    conversation_id,
                    tenant_key=tenant_key,
                    superset_user_id=owner,
                    role="user",
                    content=question,
                )
            message = conversations.append_message(
                resolved_settings,
                conversation_id,
                tenant_key=tenant_key,
                superset_user_id=owner,
                role="assistant",
                content=content or "Here is the result.",
                structured_content={
                    "kind": kind,
                    "intent": str(payload.get("intent") or ""),
                    "correlation_id": str(payload.get("correlation_id") or ""),
                    "blocks": blocks,
                },
            )
        except conversations.ConversationNotFound:
            return _admin_error("Conversation not found", 404)
        except conversations.ConversationInputError as exc:
            return _admin_error(str(exc), 400)
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(f"Could not store the turn ({_error_class(exc)})", 503)
        return jsonify({"message": message.to_public_dict()}), 201

    # --- SQL audit trail (P4-F12) ---------------------------------------

    @app.post("/ai/sql/audit")
    @require_admin_token
    def sql_audit_record():
        """Store one SQL turn — answered, refused, or failed (P4-F12).

        The proxy owns the turn and therefore writes it; this endpoint owns the
        shape, the bounds, and the sealing. A malformed record is rejected instead
        of stored, so the trail cannot be poisoned with free-form documents.
        """
        payload = request.get_json(silent=True) or {}
        owner = _conversation_owner(payload)
        if owner is None:
            return _admin_error("superset_user_id is required", 400)
        conversation_id = str(payload.get("conversation_id") or "").strip()
        if not registry.is_provider_id(conversation_id):
            return _admin_error("Conversation not found", 404)
        try:
            record = audit.record_turn(
                resolved_settings,
                conversation_id=conversation_id,
                tenant_key=str(payload.get("tenant") or registry.DEFAULT_TENANT_KEY),
                superset_user_id=owner,
                outcome=str(payload.get("outcome") or ""),
                stage=str(payload.get("stage") or ""),
                code=str(payload.get("code") or ""),
                correlation_id=str(payload.get("correlation_id") or ""),
                intent=str(payload.get("intent") or ""),
                dialect=str(payload.get("dialect") or ""),
                tables=payload.get("tables"),
                inlined=payload.get("inlined"),
                rls_applied=payload.get("rls_applied"),
                plan=payload.get("plan"),
                duration_ms=payload.get("duration_ms"),
                row_count=payload.get("row_count"),
                returned=payload.get("returned"),
                truncated=bool(payload.get("truncated")),
                cache_status=str(payload.get("cache_status") or ""),
                sql=payload.get("sql"),
            )
        except audit.AuditInputError as exc:
            return _admin_error(f"Audit record rejected ({exc})", 400)
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(f"Could not store the audit ({_error_class(exc)})", 503)
        return jsonify({"result": record.to_public_dict()}), 201

    @app.get("/ai/conversations/<conversation_id>/audit")
    @require_admin_token
    def conversation_audit(conversation_id: str):
        """The turns of one conversation, newest first — statements hidden by default.

        Redaction is decided here, once: ``include_sql`` only has an effect when the
        deployment enabled ``XP_PROMPT_ALLOW_SQL_DIAGNOSTICS``.
        """
        if not registry.is_provider_id(conversation_id):
            return _admin_error("Conversation not found", 404)
        owner = _conversation_owner(
            {"superset_user_id": request.args.get("superset_user_id")}
        )
        if owner is None:
            return _admin_error("superset_user_id is required", 400)
        wants_sql = str(request.args.get("include_sql") or "").lower() in {
            "1",
            "true",
            "yes",
        }
        reveal = wants_sql and resolved_settings.allow_sql_diagnostics
        try:
            records = audit.list_turns(
                resolved_settings,
                conversation_id,
                tenant_key=str(
                    request.args.get("tenant") or registry.DEFAULT_TENANT_KEY
                ),
                superset_user_id=owner,
                limit=int(request.args.get("limit") or audit.DEFAULT_LIST_LIMIT),
                include_sql=reveal,
            )
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(f"Could not load the audit ({_error_class(exc)})", 503)
        return (
            jsonify(
                {
                    "count": len(records),
                    "result": [
                        record.to_public_dict(include_sql=reveal) for record in records
                    ],
                }
            ),
            200,
        )

    # --- Dataset drafts (P6-F02/F04/F05/F08/F09) -------------------------

    @app.post("/ai/datasets/spec")
    @require_admin_token
    def dataset_spec_normalize():
        """Compile and judge a dataset proposal without storing anything (P6-F02).

        The proxy builds the proposal from a statement it has already executed once;
        this endpoint owns the shape, the guard on the definition, the type
        vocabulary and the quality rules, so a caller cannot decide for itself
        whether a draft is sound.
        """
        payload = request.get_json(silent=True) or {}
        if _conversation_owner(payload) is None:
            return _admin_error("superset_user_id is required", 400)
        try:
            slice_ = schema_slice.SchemaSlice.from_payload(payload)
            spec = dataset_spec.from_payload(payload.get("spec") or payload, schema=slice_)
        except dataset_spec.DatasetSpecError as exc:
            return (
                jsonify({"message": str(exc), "code": exc.code, "severity": "danger"}),
                422,
            )
        verdict = dataset_quality.check(
            spec, payload.get("profile"), confirmed=bool(payload.get("confirmed"))
        )
        return (
            jsonify(
                {
                    "spec": spec.to_public_dict(include_sql=True),
                    "quality": verdict.to_public_dict(),
                }
            ),
            200,
        )

    @app.post("/ai/datasets/drafts")
    @require_admin_token
    def dataset_draft_create():
        """Store a compiled draft — idempotently (P6-F05).

        The same ``idempotency_key`` returns the same draft, so a double-submitted
        create cannot become two datasets. A blocking quality verdict is stored with
        the draft; whether it may be saved anyway is the caller's policy decision,
        recorded rather than silently taken here.
        """
        payload = request.get_json(silent=True) or {}
        owner = _conversation_owner(payload)
        if owner is None:
            return _admin_error("superset_user_id is required", 400)
        tenant_key = str(payload.get("tenant") or registry.DEFAULT_TENANT_KEY)
        try:
            slice_ = schema_slice.SchemaSlice.from_payload(payload)
            spec = dataset_spec.from_payload(payload.get("spec") or {}, schema=slice_)
        except dataset_spec.DatasetSpecError as exc:
            return (
                jsonify({"message": str(exc), "code": exc.code, "severity": "danger"}),
                422,
            )
        verdict = dataset_quality.check(spec, payload.get("profile"))
        try:
            draft = dataset_draft.create_draft(
                resolved_settings,
                tenant_key=tenant_key,
                superset_user_id=owner,
                spec=spec,
                quality=verdict.to_public_dict(),
                conversation_id=str(payload.get("conversation_id") or "").strip()
                or None,
                job_id=str(payload.get("job_id") or "").strip() or None,
                correlation_id=str(payload.get("correlation_id") or ""),
                idempotency_key=str(payload.get("idempotency_key") or ""),
            )
        except dataset_draft.DraftInputError as exc:
            return _admin_error(f"Draft rejected ({exc.code})", 400)
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(f"Could not store the draft ({_error_class(exc)})", 503)
        return jsonify({"draft": draft.to_public_dict(include_sql=True)}), 201

    @app.get("/ai/datasets/drafts")
    @require_admin_token
    def dataset_draft_list():
        owner = _conversation_owner(
            {"superset_user_id": request.args.get("superset_user_id")}
        )
        if owner is None:
            return _admin_error("superset_user_id is required", 400)
        try:
            drafts = dataset_draft.list_drafts(
                resolved_settings,
                tenant_key=str(request.args.get("tenant") or registry.DEFAULT_TENANT_KEY),
                superset_user_id=owner,
                status=str(request.args.get("status") or ""),
                conversation_id=str(request.args.get("conversation_id") or ""),
                limit=int(request.args.get("limit") or dataset_draft.DEFAULT_LIST_LIMIT),
            )
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(f"Could not list drafts ({_error_class(exc)})", 503)
        return (
            jsonify(
                {"count": len(drafts), "result": [d.to_public_dict() for d in drafts]}
            ),
            200,
        )

    @app.get("/ai/datasets/drafts/<draft_id>")
    @require_admin_token
    def dataset_draft_detail(draft_id: str):
        owner = _conversation_owner(
            {"superset_user_id": request.args.get("superset_user_id")}
        )
        if owner is None:
            return _admin_error("superset_user_id is required", 400)
        reveal = str(request.args.get("include_sql") or "").lower() in {"1", "true"}
        try:
            draft = dataset_draft.get_draft(
                resolved_settings,
                draft_id,
                tenant_key=str(request.args.get("tenant") or registry.DEFAULT_TENANT_KEY),
                superset_user_id=owner,
            )
        except dataset_draft.DraftNotFound:
            return _admin_error("Draft not found", 404)
        return jsonify({"draft": draft.to_public_dict(include_sql=reveal)}), 200

    @app.post("/ai/datasets/drafts/<draft_id>")
    @require_admin_token
    def dataset_draft_update(draft_id: str):
        """Replace an open draft's proposal; a stale writer is refused (P6-F09)."""
        payload = request.get_json(silent=True) or {}
        owner = _conversation_owner(payload)
        if owner is None:
            return _admin_error("superset_user_id is required", 400)
        tenant_key = str(payload.get("tenant") or registry.DEFAULT_TENANT_KEY)
        try:
            slice_ = schema_slice.SchemaSlice.from_payload(payload)
            spec = dataset_spec.from_payload(payload.get("spec") or {}, schema=slice_)
        except dataset_spec.DatasetSpecError as exc:
            return (
                jsonify({"message": str(exc), "code": exc.code, "severity": "danger"}),
                422,
            )
        verdict = dataset_quality.check(spec, payload.get("profile"))
        expected = payload.get("expected_version")
        try:
            draft = dataset_draft.update_draft(
                resolved_settings,
                draft_id,
                tenant_key=tenant_key,
                superset_user_id=owner,
                spec=spec,
                quality=verdict.to_public_dict(),
                expected_version=int(expected) if expected is not None else None,
            )
        except dataset_draft.DraftNotFound:
            return _admin_error("Draft not found", 404)
        except dataset_draft.DraftConflict as exc:
            return (
                jsonify(
                    {
                        "message": _draft_conflict_message(exc),
                        "code": "stale_draft",
                        "current_version": exc.actual,
                        "severity": "warning",
                    }
                ),
                409,
            )
        except dataset_draft.DraftStateError as exc:
            return _admin_error(f"Draft refused ({exc.code}): {exc}", 409)
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(f"Could not update the draft ({_error_class(exc)})", 503)
        return jsonify({"draft": draft.to_public_dict(include_sql=True)}), 200

    @app.post("/ai/datasets/drafts/<draft_id>/saved")
    @require_admin_token
    def dataset_draft_mark_saved(draft_id: str):
        """Pin the draft to the dataset that now exists in Superset (P6-F08)."""
        payload = request.get_json(silent=True) or {}
        owner = _conversation_owner(payload)
        if owner is None:
            return _admin_error("superset_user_id is required", 400)
        try:
            draft = dataset_draft.mark_saved(
                resolved_settings,
                draft_id,
                tenant_key=str(payload.get("tenant") or registry.DEFAULT_TENANT_KEY),
                superset_user_id=owner,
                dataset_id=int(payload.get("dataset_id") or 0),
                owners=payload.get("owners"),
            )
        except dataset_draft.DraftInputError as exc:
            return _admin_error(str(exc), 400)
        except dataset_draft.DraftNotFound:
            return _admin_error("Draft not found", 404)
        except dataset_draft.DraftStateError as exc:
            return _admin_error(f"Draft refused ({exc.code}): {exc}", 409)
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(f"Could not save the draft ({_error_class(exc)})", 503)
        return jsonify({"draft": draft.to_public_dict()}), 200

    @app.post("/ai/datasets/drafts/<draft_id>/scrap")
    @require_admin_token
    def dataset_draft_scrap(draft_id: str):
        """Close a draft that produced nothing, so no half-object is left behind."""
        payload = request.get_json(silent=True) or {}
        owner = _conversation_owner(payload)
        if owner is None:
            return _admin_error("superset_user_id is required", 400)
        try:
            draft = dataset_draft.scrap_draft(
                resolved_settings,
                draft_id,
                tenant_key=str(payload.get("tenant") or registry.DEFAULT_TENANT_KEY),
                superset_user_id=owner,
                reason=str(payload.get("reason") or ""),
            )
        except dataset_draft.DraftNotFound:
            return _admin_error("Draft not found", 404)
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(f"Could not scrap the draft ({_error_class(exc)})", 503)
        return jsonify({"draft": draft.to_public_dict()}), 200

    # --- Chart recommendations (P7-F01/F02/F03) --------------------------

    @app.post("/ai/charts/recommend")
    @require_admin_token
    def chart_recommend():
        """Recommend visualizations for a dataset, and say why the rest were dropped.

        The proxy reports what this deployment can render (it is the only component
        that can ask the deployment) and what was measured about the fields; this
        endpoint owns the rule engine and the control contracts, so a recommendation
        can never name a viz type the deployment does not have or a control that viz
        type does not read.
        """
        payload = request.get_json(silent=True) or {}
        if _conversation_owner(payload) is None:
            return _admin_error("superset_user_id is required", 400)
        catalog = chart_catalog.Catalog.from_payload(payload)
        profile = chart_profile.profile(
            payload.get("columns"),
            measured=payload.get("measured"),
            row_count=payload.get("row_count") or 0,
            grain=payload.get("grain"),
        )
        recommendation = chart_rules.recommend(
            profile,
            catalog,
            row_limit=payload.get("row_limit") or chart_rules.DEFAULT_ROW_LIMIT,
        )
        ranking = chart_ranking.Ranking(
            order=tuple(candidate.id for candidate in recommendation.candidates)
        )
        if payload.get("rank_with_model"):
            ranking = chart_ranking.rank(
                resolved_settings,
                tenant_key=str(
                    payload.get("tenant") or registry.DEFAULT_TENANT_KEY
                ),
                candidates=recommendation.candidates,
                question=str(payload.get("question") or ""),
            )
            recommendation = chart_rules.Recommendation(
                candidates=chart_ranking.apply_order(
                    recommendation.candidates, ranking
                ),
                eliminated=recommendation.eliminated,
                profile_fingerprint=recommendation.profile_fingerprint,
            )
        logger.info(
            "chart_recommend outcome=%s candidates=%d available=%d ranking=%s",
            "ok" if recommendation.candidates else "empty",
            len(recommendation.candidates),
            len(catalog.available),
            ranking.source,
        )
        body = recommendation.to_public_dict()
        body["catalog"] = catalog.to_public_dict()
        body["ranking"] = ranking.to_public_dict()
        if payload.get("include_profile"):
            body["profile"] = profile.to_public_dict()
        return jsonify(body), 200

    @app.get("/ai/charts/contracts")
    @require_admin_token
    def chart_contracts():
        """What each supported visualization accepts — the operator's reference."""
        return (
            jsonify(
                {
                    "result": [
                        contract.to_public_dict()
                        for contract in chart_catalog.VIZ_CONTRACTS.values()
                    ]
                }
            ),
            200,
        )

    # --- Chart drafts (P7-F05/F09) ---------------------------------------

    @app.post("/ai/charts/drafts")
    @require_admin_token
    def chart_draft_create():
        """Store a validated chart configuration, idempotently.

        The configuration must pass the target visualization's control contract, so a
        draft cannot carry a control its viz type does not read.
        """
        payload = request.get_json(silent=True) or {}
        owner = _conversation_owner(payload)
        if owner is None:
            return _admin_error("superset_user_id is required", 400)
        catalog = chart_catalog.Catalog.from_payload(payload)
        try:
            draft = chart_draft.create_draft(
                resolved_settings,
                tenant_key=str(payload.get("tenant") or registry.DEFAULT_TENANT_KEY),
                superset_user_id=owner,
                catalog=catalog,
                viz_type=str(payload.get("viz_type") or ""),
                title=str(payload.get("title") or ""),
                dataset_id=int(payload.get("dataset_id") or 0),
                dataset_name=str(payload.get("dataset_name") or ""),
                database=str(payload.get("database") or ""),
                form_data=payload.get("form_data"),
                rationale=str(payload.get("rationale") or ""),
                candidate_id=str(payload.get("candidate_id") or ""),
                ranking_source=str(payload.get("ranking_source") or ""),
                description=str(payload.get("description") or ""),
                profile_fingerprint=str(payload.get("profile_fingerprint") or ""),
                conversation_id=str(payload.get("conversation_id") or "").strip()
                or None,
                job_id=str(payload.get("job_id") or "").strip() or None,
                correlation_id=str(payload.get("correlation_id") or ""),
                idempotency_key=str(payload.get("idempotency_key") or ""),
            )
        except chart_draft.ChartDraftError as exc:
            return (
                jsonify({"message": str(exc), "code": exc.code, "severity": "danger"}),
                422,
            )
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(f"Could not store the draft ({_error_class(exc)})", 503)
        return jsonify({"draft": draft.to_public_dict()}), 201

    @app.get("/ai/charts/drafts")
    @require_admin_token
    def chart_draft_list():
        owner = _conversation_owner(
            {"superset_user_id": request.args.get("superset_user_id")}
        )
        if owner is None:
            return _admin_error("superset_user_id is required", 400)
        try:
            drafts = chart_draft.list_drafts(
                resolved_settings,
                tenant_key=str(
                    request.args.get("tenant") or registry.DEFAULT_TENANT_KEY
                ),
                superset_user_id=owner,
                status=str(request.args.get("status") or ""),
                conversation_id=str(request.args.get("conversation_id") or ""),
                dataset_id=(
                    int(request.args["dataset_id"])
                    if str(request.args.get("dataset_id") or "").isdigit()
                    else None
                ),
                limit=int(request.args.get("limit") or chart_draft.DEFAULT_LIST_LIMIT),
            )
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(f"Could not list drafts ({_error_class(exc)})", 503)
        return (
            jsonify(
                {"count": len(drafts), "result": [draft.to_public_dict() for draft in drafts]}
            ),
            200,
        )

    @app.get("/ai/charts/drafts/<draft_id>")
    @require_admin_token
    def chart_draft_detail(draft_id: str):
        owner = _conversation_owner(
            {"superset_user_id": request.args.get("superset_user_id")}
        )
        if owner is None:
            return _admin_error("superset_user_id is required", 400)
        try:
            draft = chart_draft.get_draft(
                resolved_settings,
                draft_id,
                tenant_key=str(
                    request.args.get("tenant") or registry.DEFAULT_TENANT_KEY
                ),
                superset_user_id=owner,
            )
        except chart_draft.ChartDraftNotFound:
            return _admin_error("Draft not found", 404)
        return jsonify({"draft": draft.to_public_dict()}), 200

    @app.post("/ai/charts/drafts/<draft_id>")
    @require_admin_token
    def chart_draft_update(draft_id: str):
        """Replace an open draft's configuration; a stale writer is refused (P7-F07)."""
        payload = request.get_json(silent=True) or {}
        owner = _conversation_owner(payload)
        if owner is None:
            return _admin_error("superset_user_id is required", 400)
        catalog = chart_catalog.Catalog.from_payload(payload)
        expected = payload.get("expected_version")
        try:
            draft = chart_draft.update_draft(
                resolved_settings,
                draft_id,
                tenant_key=str(payload.get("tenant") or registry.DEFAULT_TENANT_KEY),
                superset_user_id=owner,
                catalog=catalog,
                viz_type=str(payload.get("viz_type") or ""),
                title=str(payload.get("title") or ""),
                form_data=payload.get("form_data"),
                rationale=str(payload.get("rationale") or ""),
                expected_version=int(expected) if expected is not None else None,
            )
        except chart_draft.ChartDraftNotFound:
            return _admin_error("Draft not found", 404)
        except chart_draft.ChartDraftConflict as exc:
            return (
                jsonify(
                    {
                        "message": _draft_conflict_message(exc),
                        "code": "stale_draft",
                        "current_version": exc.actual,
                        "severity": "warning",
                    }
                ),
                409,
            )
        except chart_draft.ChartDraftError as exc:
            status = 409 if exc.code == "not_open" else 422
            return (
                jsonify({"message": str(exc), "code": exc.code, "severity": "danger"}),
                status,
            )
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(f"Could not update the draft ({_error_class(exc)})", 503)
        return jsonify({"draft": draft.to_public_dict()}), 200

    @app.post("/ai/charts/drafts/<draft_id>/runs")
    @require_admin_token
    def chart_draft_run(draft_id: str):
        """Count one preview render (P7-F06); accountability, not bookkeeping for its own sake."""
        payload = request.get_json(silent=True) or {}
        owner = _conversation_owner(payload)
        if owner is None:
            return _admin_error("superset_user_id is required", 400)
        chart_draft.record_run(
            resolved_settings,
            draft_id,
            tenant_key=str(payload.get("tenant") or registry.DEFAULT_TENANT_KEY),
            superset_user_id=owner,
        )
        return jsonify({"recorded": True}), 200

    @app.post("/ai/charts/drafts/<draft_id>/saved")
    @require_admin_token
    def chart_draft_mark_saved(draft_id: str):
        """Pin the draft to the chart that now exists in Superset (P7-F09)."""
        payload = request.get_json(silent=True) or {}
        owner = _conversation_owner(payload)
        if owner is None:
            return _admin_error("superset_user_id is required", 400)
        try:
            draft = chart_draft.mark_saved(
                resolved_settings,
                draft_id,
                tenant_key=str(payload.get("tenant") or registry.DEFAULT_TENANT_KEY),
                superset_user_id=owner,
                slice_id=int(payload.get("slice_id") or 0),
            )
        except chart_draft.ChartDraftError as exc:
            status = 404 if exc.code == "not_found" else 422
            return _admin_error(f"Draft refused ({exc.code}): {exc}", status)
        except chart_draft.ChartDraftNotFound:
            return _admin_error("Draft not found", 404)
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(f"Could not save the draft ({_error_class(exc)})", 503)
        return jsonify({"draft": draft.to_public_dict()}), 200

    @app.post("/ai/charts/drafts/<draft_id>/scrap")
    @require_admin_token
    def chart_draft_scrap(draft_id: str):
        """Close a draft that produced nothing (P7-F10)."""
        payload = request.get_json(silent=True) or {}
        owner = _conversation_owner(payload)
        if owner is None:
            return _admin_error("superset_user_id is required", 400)
        try:
            draft = chart_draft.scrap_draft(
                resolved_settings,
                draft_id,
                tenant_key=str(payload.get("tenant") or registry.DEFAULT_TENANT_KEY),
                superset_user_id=owner,
                reason=str(payload.get("reason") or ""),
            )
        except chart_draft.ChartDraftNotFound:
            return _admin_error("Draft not found", 404)
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(f"Could not scrap the draft ({_error_class(exc)})", 503)
        return jsonify({"draft": draft.to_public_dict()}), 200

    # --- Dashboard planning and preview (P8-F01..F07) ---------------------

    @app.post("/ai/dashboards/plan")
    @require_admin_token
    def dashboard_plan_build():
        """Plan a dashboard from a goal, reusing whatever already exists.

        The proxy owns the Superset side (which datasets and charts the principal may
        see, and which viz types this deployment can draw). This endpoint owns the
        planning rules: relevance, family diversity, reuse before regeneration, and the
        explicit "needs generation" list that a caller must satisfy before publishing.
        """
        payload = request.get_json(silent=True) or {}
        if _conversation_owner(payload) is None:
            return _admin_error("superset_user_id is required", 400)
        plan = dashboard_plan.build(
            goal=str(payload.get("goal") or ""),
            datasets=payload.get("datasets") or (),
            charts=payload.get("charts") or (),
            candidates=payload.get("candidates") or {},
            catalog=chart_catalog.Catalog.from_payload(payload),
            time_grain=payload.get("time_grain"),
        )
        logger.info(
            "dashboard_plan outcome=%s slots=%d reuse=%d needs=%d",
            "ok" if not plan.empty else "empty",
            len(plan.slots),
            plan.reuse_count,
            len(plan.needs),
        )
        return jsonify(plan.to_public_dict()), 200

    @app.post("/ai/dashboards/layout")
    @require_admin_token
    def dashboard_layout_build():
        """Turn saved charts into validated position metadata plus safe native filters.

        Everything here is a check: a slot without a saved chart cannot be drawn, a row
        may not exceed twelve columns, and a filter is only emitted for a column that at
        least two in-scope charts share. Invalid input returns the problems instead of a
        layout that would fail later in the browser.
        """
        payload = request.get_json(silent=True) or {}
        if _conversation_owner(payload) is None:
            return _admin_error("superset_user_id is required", 400)
        slots = payload.get("slots") or ()
        position, problems = dashboard_layout.build_position(
            slots,
            title=str(payload.get("title") or ""),
        )
        validation = dashboard_layout.validate_position(position)
        filters = dashboard_layout.plan_filters(
            slots,
            columns=payload.get("columns") or {},
            max_filters=int(payload.get("max_filters") or dashboard_layout.MAX_FILTERS),
        )
        body = {
            "position": position,
            "problems": list(problems),
            "validation": list(validation),
            "valid": not problems and not validation,
        }
        body.update(filters.to_public_dict())
        logger.info(
            "dashboard_layout outcome=%s rows=%d filters=%d",
            "ok" if body["valid"] else "invalid",
            len(position.get("GRID_ID", {}).get("children", ())),
            len(filters.filters),
        )
        return jsonify(body), 200 if body["valid"] else 422

    @app.post("/ai/dashboards/preview")
    @require_admin_token
    def dashboard_preview_build():
        """Validate every chart-data result and assemble a read-only preview.

        Superset has already rendered the slots in the caller's security context. This
        endpoint owns the bounded result policy, generated metadata and accessible chart
        context. It refuses a missing or extra render, so ``complete: true`` can never
        describe a partial dashboard.
        """
        payload = request.get_json(silent=True) or {}
        if _conversation_owner(payload) is None:
            return _admin_error("superset_user_id is required", 400)
        try:
            preview = dashboard_preview.build(
                title=str(payload.get("title") or ""),
                goal=str(payload.get("goal") or ""),
                audience=str(payload.get("audience") or "unspecified"),
                slots=payload.get("slots") or (),
                renders=payload.get("renders") or (),
            )
        except dashboard_preview.DashboardPreviewError as exc:
            return (
                jsonify(
                    {
                        "message": str(exc),
                        "code": exc.code,
                        "severity": "danger",
                    }
                ),
                422,
            )
        logger.info(
            "dashboard_preview outcome=ok slots=%d datasets=%d",
            len(preview.slots),
            preview.metadata.dataset_count,
        )
        return jsonify(preview.to_public_dict()), 200

    @app.post("/ai/dashboards/publish")
    @require_admin_token
    def dashboard_publish_start():
        """Open a publish attempt, refusing access the caller may not assign.

        Superset has already created any chart a generated slot needed, so the one
        thing this endpoint must not do is fail silently: it decides the access the
        publish will carry, checks that an update is not aimed at an edited dashboard,
        and records the attempt with every id it already produced (P8-F08..F11).
        """
        payload = request.get_json(silent=True) or {}
        owner = _conversation_owner(payload)
        if owner is None:
            return _admin_error("superset_user_id is required", 400)
        tenant_key = str(payload.get("tenant") or registry.DEFAULT_TENANT_KEY)
        try:
            wanted = dashboard_publish.Request.parse(payload)
            policy = dashboard_publish.Policy.from_payload(payload.get("policy"))
            live_version = 0
            if wanted.mode == dashboard_publish.UPDATE and (
                wanted.target_dashboard_id is not None
            ):
                live_version = dashboard_publish.version_of(
                    resolved_settings,
                    tenant_key=tenant_key,
                    superset_user_id=owner,
                    dashboard_id=wanted.target_dashboard_id,
                )
                # Drift protection is on by default: without an explicit
                # expectation, "unchanged since it was published" is the rule.
                if not wanted.expected_fingerprint:
                    previous = dashboard_publish.latest(
                        resolved_settings,
                        tenant_key=tenant_key,
                        superset_user_id=owner,
                        dashboard_id=wanted.target_dashboard_id,
                    )
                    if previous and previous["position_fingerprint"]:
                        wanted = replace(
                            wanted,
                            expected_fingerprint=previous["position_fingerprint"],
                        )
            decision = dashboard_publish.decide(
                wanted,
                policy,
                caller_id=owner,
                position=payload.get("position"),
                live_position=payload.get("live_position"),
                live_version=live_version,
            )
            record, replayed = dashboard_publish.start(
                resolved_settings,
                wanted,
                decision,
                tenant_key=tenant_key,
                superset_user_id=owner,
                correlation_id=str(payload.get("correlation_id") or ""),
                conversation_id=_optional_uuid(payload.get("conversation_id")),
                job_id=_optional_uuid(payload.get("job_id")),
            )
        except dashboard_publish.DashboardPublishError as exc:
            logger.info(
                "dashboard_publish refused code=%s mode=%s",
                exc.code,
                str(payload.get("mode") or "create"),
            )
            return jsonify(
                {"message": str(exc), "code": exc.code, "severity": "danger"}
            ), 422
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(
                f"Could not open publish ({_error_class(exc)})", 503
            )
        body = dict(record)
        body.update({"replayed": replayed, **decision.to_public_dict()})
        logger.info(
            "dashboard_publish outcome=%s publish=%s mode=%s replayed=%s",
            record["status"],
            record["id"],
            record["mode"],
            replayed,
        )
        return jsonify(body), 200 if replayed else 201

    @app.post("/ai/dashboards/publish/<publish_id>/published")
    @require_admin_token
    def dashboard_publish_complete(publish_id: str):
        """Record the dashboard and objects a successful attempt created."""
        if not _is_uuid(publish_id):
            return _admin_error("Publish not found", 404)
        payload = request.get_json(silent=True) or {}
        owner = _conversation_owner(payload)
        if owner is None:
            return _admin_error("superset_user_id is required", 400)
        tenant_key = str(payload.get("tenant") or registry.DEFAULT_TENANT_KEY)
        try:
            record = dashboard_publish.complete(
                resolved_settings,
                publish_id,
                tenant_key=tenant_key,
                superset_user_id=owner,
                dashboard_id=payload.get("dashboard_id"),
                dashboard_slug=str(payload.get("dashboard_slug") or ""),
                position=payload.get("position"),
                created=payload.get("created") or (),
                cleanup=payload.get("cleanup") or (),
            )
        except dashboard_publish.DashboardPublishError as exc:
            return jsonify(
                {"message": str(exc), "code": exc.code, "severity": "danger"}
            ), 422
        except dashboard_publish.DashboardPublishNotFound:
            return _admin_error("Publish not found", 404)
        except dashboard_publish.DashboardPublishStateError as exc:
            return _admin_error(str(exc), 409)
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(
                f"Could not record publish ({_error_class(exc)})", 503
            )
        return jsonify(record), 200

    @app.post("/ai/dashboards/publish/<publish_id>/failed")
    @require_admin_token
    def dashboard_publish_fail(publish_id: str):
        """Close a failed attempt, keeping one cleanup outcome per object."""
        if not _is_uuid(publish_id):
            return _admin_error("Publish not found", 404)
        payload = request.get_json(silent=True) or {}
        owner = _conversation_owner(payload)
        if owner is None:
            return _admin_error("superset_user_id is required", 400)
        tenant_key = str(payload.get("tenant") or registry.DEFAULT_TENANT_KEY)
        try:
            record = dashboard_publish.fail(
                resolved_settings,
                publish_id,
                tenant_key=tenant_key,
                superset_user_id=owner,
                error_code=str(payload.get("error_code") or "publish_failed"),
                error_message=str(payload.get("error_message") or ""),
                created=payload.get("created") or (),
                cleanup=payload.get("cleanup") or (),
            )
        except dashboard_publish.DashboardPublishNotFound:
            return _admin_error("Publish not found", 404)
        except dashboard_publish.DashboardPublishStateError as exc:
            return _admin_error(str(exc), 409)
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(
                f"Could not record publish failure ({_error_class(exc)})", 503
            )
        return jsonify(record), 200

    @app.get("/ai/dashboards/publish/<publish_id>")
    @require_admin_token
    def dashboard_publish_detail(publish_id: str):
        """What did this attempt create, and what became of it (P8-F10)."""
        if not _is_uuid(publish_id):
            return _admin_error("Publish not found", 404)
        owner = _conversation_owner(request.args.to_dict())
        if owner is None:
            return _admin_error("superset_user_id is required", 400)
        tenant_key = request.args.get("tenant") or registry.DEFAULT_TENANT_KEY
        try:
            record = dashboard_publish.get(
                resolved_settings,
                publish_id,
                tenant_key=tenant_key,
                superset_user_id=owner,
            )
        except dashboard_publish.DashboardPublishNotFound:
            return _admin_error("Publish not found", 404)
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(
                f"Could not load publish ({_error_class(exc)})", 503
            )
        return jsonify(record), 200

    # --- Usage ledger and prices (P9-F01..F05, F07, F10) -----------------

    @app.get("/ai/usage/summary")
    @require_admin_token
    def usage_summary():
        """Totals behind the admin cards, with the cost labelled estimated.

        The numbers are computed from a published price list, not read from a
        provider invoice, so the response says so rather than letting a caller
        assume otherwise (P9-F08).
        """
        tenant_key = request.args.get("tenant") or registry.DEFAULT_TENANT_KEY
        raw_groups = request.args.getlist("group_by") or request.args.get(
            "group_by", ""
        ).split(",")
        try:
            body = usage.summary(
                resolved_settings,
                tenant_key=tenant_key,
                group_by=[group for group in raw_groups if group],
                since=_parse_timestamp(request.args.get("since")),
                module=request.args.get("module") or None,
                superset_user_id=(
                    int(request.args["superset_user_id"])
                    if request.args.get("superset_user_id")
                    else None
                ),
            )
        except usage.UsageError as exc:
            return jsonify(
                {"message": str(exc), "code": exc.code, "severity": "danger"}
            ), 422
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(f"Could not read usage ({_error_class(exc)})", 503)
        return jsonify(body), 200

    @app.get("/ai/usage/attempts")
    @require_admin_token
    def usage_attempts():
        """The newest attempts, one row per provider call (P9-F02)."""
        tenant_key = request.args.get("tenant") or registry.DEFAULT_TENANT_KEY
        limit = request.args.get("limit") or usage.DEFAULT_LIST_LIMIT
        try:
            rows = usage.attempts(
                resolved_settings,
                tenant_key=tenant_key,
                correlation_id=request.args.get("correlation_id") or None,
                module=request.args.get("module") or None,
                limit=int(limit),
            )
        except (TypeError, ValueError):
            return _admin_error("limit must be an integer", 400)
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(
                f"Could not read attempts ({_error_class(exc)})", 503
            )
        return jsonify({"count": len(rows), "result": rows}), 200

    @app.get("/ai/usage/reconciliation")
    @require_admin_token
    def usage_reconciliation():
        """Ledger quality: missing usage, unpriced models, impossible counts."""
        tenant_key = request.args.get("tenant") or registry.DEFAULT_TENANT_KEY
        try:
            body = usage.reconcile(resolved_settings, tenant_key=tenant_key)
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(
                f"Could not reconcile usage ({_error_class(exc)})", 503
            )
        return jsonify(body), 200

    @app.get("/ai/usage/prices")
    @require_admin_token
    def usage_price_list():
        tenant_key = request.args.get("tenant") or registry.DEFAULT_TENANT_KEY
        try:
            rows = usage.list_prices(
                resolved_settings,
                tenant_key=tenant_key,
                provider=request.args.get("provider") or None,
                model=request.args.get("model") or None,
            )
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(f"Could not read prices ({_error_class(exc)})", 503)
        return (
            jsonify(
                {"count": len(rows), "result": [row.to_public_dict() for row in rows]}
            ),
            200,
        )

    @app.post("/ai/usage/prices")
    @require_admin_token
    def usage_price_publish():
        """Publish a new price version; an old one is closed, never edited."""
        payload = request.get_json(silent=True) or {}
        tenant_key = str(payload.get("tenant") or registry.DEFAULT_TENANT_KEY)
        try:
            price = usage.ModelPrice(
                provider=str(payload.get("provider") or "").strip(),
                model=str(payload.get("model") or "").strip(),
                version=int(payload.get("version") or 1),
                currency=str(payload.get("currency") or "").strip().upper(),
                unit_tokens=int(payload.get("unit_tokens") or usage.MICROS_PER_UNIT),
                input_micros_per_unit=int(
                    payload.get("input_micros_per_unit") or 0
                ),
                output_micros_per_unit=int(
                    payload.get("output_micros_per_unit") or 0
                ),
                cached_input_micros_per_unit=_optional_int(
                    payload.get("cached_input_micros_per_unit")
                ),
                reasoning_micros_per_unit=_optional_int(
                    payload.get("reasoning_micros_per_unit")
                ),
                effective_from=_parse_timestamp(payload.get("effective_from")),
                source=str(payload.get("source") or ""),
                source_url=str(payload.get("source_url") or ""),
            )
            stored = usage.publish_price(
                resolved_settings, price, tenant_key=tenant_key
            )
        except usage.UsageError as exc:
            return jsonify(
                {"message": str(exc), "code": exc.code, "severity": "danger"}
            ), 422
        except (TypeError, ValueError):
            return _admin_error("price fields must be integers", 400)
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(
                f"Could not publish price ({_error_class(exc)})", 503
            )
        return jsonify(stored.to_public_dict()), 201

    @app.get("/ai/usage/outbox")
    @require_admin_token
    def usage_outbox_status():
        """How much usage work is waiting, and what gave up (P9-F06)."""
        tenant_key = request.args.get("tenant") or registry.DEFAULT_TENANT_KEY
        try:
            tenant_id = usage.tenant_identifier(resolved_settings, tenant_key)
            counts = outbox.pending(resolved_settings, tenant_id=tenant_id)
            failures = outbox.dead_letters(
                resolved_settings, tenant_id=tenant_id, limit=20
            )
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(f"Could not read outbox ({_error_class(exc)})", 503)
        return (
            jsonify(
                {
                    "tenant": tenant_key,
                    "counts": counts,
                    "dead_letters": [event.to_public_dict() for event in failures],
                    "count": len(failures),
                }
            ),
            200,
        )

    @app.post("/ai/usage/outbox/drain")
    @require_admin_token
    def usage_outbox_drain():
        """Apply waiting usage events now, instead of waiting for the worker."""
        payload = request.get_json(silent=True) or {}
        try:
            limit = int(payload.get("limit") or outbox.DEFAULT_BATCH)
        except (TypeError, ValueError):
            return _admin_error("limit must be an integer", 400)
        try:
            outcome = usage.drain(resolved_settings, limit=limit)
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(f"Could not drain outbox ({_error_class(exc)})", 503)
        return jsonify(outcome), 200

    @app.post("/ai/usage/outbox/<event_id>/requeue")
    @require_admin_token
    def usage_outbox_requeue(event_id: str):
        """Requeue a dead-lettered event once the cause has been fixed."""
        if not _is_uuid(event_id):
            return _admin_error("Event not found", 404)
        try:
            event = outbox.requeue(resolved_settings, event_id)
        except outbox.OutboxError as exc:
            return jsonify(
                {"message": str(exc), "code": exc.code, "severity": "warning"}
            ), 409
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(f"Could not requeue ({_error_class(exc)})", 503)
        return jsonify(event.to_public_dict()), 200

    # --- Budgets and alerts (P9-F09) -------------------------------------

    @app.get("/ai/usage/budgets")
    @require_admin_token
    def usage_budget_list():
        """Every budget with its live window, so the admin sees headroom."""
        tenant_key = request.args.get("tenant") or registry.DEFAULT_TENANT_KEY
        try:
            body = budgets.status(resolved_settings, tenant_key=tenant_key)
        except budgets.BudgetError as exc:
            return jsonify(
                {"message": str(exc), "code": exc.code, "severity": "danger"}
            ), 422
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(f"Could not read budgets ({_error_class(exc)})", 503)
        return jsonify(body), 200

    @app.post("/ai/usage/budgets")
    @require_admin_token
    def usage_budget_define():
        """Create or replace one budget; the scope decides what it constrains."""
        payload = request.get_json(silent=True) or {}
        tenant_key = str(payload.get("tenant") or registry.DEFAULT_TENANT_KEY)
        try:
            budget = budgets.define(
                resolved_settings,
                tenant_key=tenant_key,
                scope=str(payload.get("scope") or ""),
                period=str(payload.get("period") or ""),
                scope_ref=str(payload.get("scope_ref") or ""),
                cost_micros_limit=_optional_int(payload.get("cost_micros_limit")),
                tokens_limit=_optional_int(payload.get("tokens_limit")),
                warn_percent=int(
                    payload.get("warn_percent") or budgets.DEFAULT_WARN_PERCENT
                ),
                hard_stop=bool(payload.get("hard_stop", True)),
                enabled=bool(payload.get("enabled", True)),
                currency=str(payload.get("currency") or ""),
            )
        except budgets.BudgetError as exc:
            return jsonify(
                {"message": str(exc), "code": exc.code, "severity": "danger"}
            ), 422
        except (TypeError, ValueError):
            return _admin_error("budget fields must be integers", 400)
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(f"Could not save budget ({_error_class(exc)})", 503)
        return jsonify(budget.to_public_dict()), 201

    @app.delete("/ai/usage/budgets/<budget_id>")
    @require_admin_token
    def usage_budget_delete(budget_id: str):
        if not _is_uuid(budget_id):
            return _admin_error("Budget not found", 404)
        tenant_key = request.args.get("tenant") or registry.DEFAULT_TENANT_KEY
        try:
            removed = budgets.delete_budget(
                resolved_settings, budget_id, tenant_key=tenant_key
            )
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(f"Could not delete budget ({_error_class(exc)})", 503)
        if not removed:
            return _admin_error("Budget not found", 404)
        return jsonify({"deleted": budget_id}), 200

    # --- Provider billing imports (P9-F10) -------------------------------

    @app.get("/ai/usage/billing/imports")
    @require_admin_token
    def usage_billing_imports():
        tenant_key = request.args.get("tenant") or registry.DEFAULT_TENANT_KEY
        try:
            rows = billing.list_imports(resolved_settings, tenant_key=tenant_key)
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(
                f"Could not read billing imports ({_error_class(exc)})", 503
            )
        return (
            jsonify(
                {"count": len(rows), "result": [row.to_public_dict() for row in rows]}
            ),
            200,
        )

    @app.post("/ai/usage/billing/imports")
    @require_admin_token
    def usage_billing_import():
        """Store one provider billing export and match it to recorded attempts.

        The estimate on each attempt is left untouched: an invoice is evidence, not
        a correction, and overwriting the history would make the variance
        impossible to see (P9-F10).
        """
        payload = request.get_json(silent=True) or {}
        tenant_key = str(payload.get("tenant") or registry.DEFAULT_TENANT_KEY)
        period_start = _parse_timestamp(payload.get("period_start"))
        period_end = _parse_timestamp(payload.get("period_end"))
        if period_start is None or period_end is None:
            return _admin_error(
                "period_start and period_end must be ISO timestamps", 400
            )
        rows = payload.get("rows")
        if not isinstance(rows, list):
            return _admin_error("rows must be a list", 400)
        try:
            record, replayed = billing.record_import(
                resolved_settings,
                tenant_key=tenant_key,
                provider=str(payload.get("provider") or ""),
                period_start=period_start,
                period_end=period_end,
                currency=str(payload.get("currency") or ""),
                source=str(payload.get("source") or ""),
                rows=rows,
            )
        except billing.BillingError as exc:
            return jsonify(
                {"message": str(exc), "code": exc.code, "severity": "danger"}
            ), 422
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(
                f"Could not import billing ({_error_class(exc)})", 503
            )
        body = dict(record.to_public_dict())
        body["replayed"] = replayed
        return jsonify(body), 200 if replayed else 201

    @app.get("/ai/usage/billing/source")
    @require_admin_token
    def usage_billing_source():
        """Whether an export source is configured; never the URL or the token."""
        return jsonify(billing.source_status(resolved_settings)), 200

    @app.post("/ai/usage/billing/fetch")
    @require_admin_token
    def usage_billing_fetch():
        """Fetch the provider's billing export now instead of waiting for the job."""
        payload = request.get_json(silent=True) or {}
        tenant_key = str(payload.get("tenant") or registry.DEFAULT_TENANT_KEY)
        try:
            outcome = billing.fetch_export(
                resolved_settings,
                tenant_key=tenant_key,
                provider=payload.get("provider"),
                currency=payload.get("currency"),
                period_start=_parse_timestamp(payload.get("period_start")),
                period_end=_parse_timestamp(payload.get("period_end")),
            )
        except billing.BillingError as exc:
            status = 409 if exc.code == "not_configured" else 422
            return jsonify(
                {"message": str(exc), "code": exc.code, "severity": "danger"}
            ), status
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(f"Could not fetch billing ({_error_class(exc)})", 503)
        return jsonify(outcome), 200 if outcome["replayed"] else 201

    @app.get("/ai/usage/billing/imports/<import_id>/variance")
    @require_admin_token
    def usage_billing_variance(import_id: str):
        """Estimated against billed, with the unmatched counts that explain it."""
        if not _is_uuid(import_id):
            return _admin_error("Billing import not found", 404)
        tenant_key = request.args.get("tenant") or registry.DEFAULT_TENANT_KEY
        try:
            body = billing.variance(
                resolved_settings, import_id, tenant_key=tenant_key
            )
        except billing.BillingError as exc:
            return jsonify(
                {"message": str(exc), "code": exc.code, "severity": "danger"}
            ), 404
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(
                f"Could not compare billing ({_error_class(exc)})", 503
            )
        return jsonify(body), 200

    # --- Administrative configuration (P10) ------------------------------

    @app.get("/ai/settings")
    @require_admin_token
    def settings_list():
        """The registry, the stored rows and the effective value of each setting.

        Configuration is described, not guessed: a caller can see a setting's
        type, bounds and whether changing it needs a restart, so the UI does not
        have to hardcode what the service already knows (P10-F01/F11).
        """
        tenant_key = request.args.get("tenant") or registry.DEFAULT_TENANT_KEY
        try:
            resolutions = settings_registry.load(resolved_settings, tenant_key=tenant_key)
            rows = settings_registry.stored(resolved_settings, tenant_key=tenant_key)
        except settings_registry.SettingError as exc:
            return jsonify(
                {"message": str(exc), "code": exc.code, "severity": "danger"}
            ), 422
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(f"Could not read settings ({_error_class(exc)})", 503)
        by_scope = {(row.scope, row.key): row for row in rows}
        entries = [
            {
                **setting.to_public_dict(),
                "effective": resolutions[setting.key].value,
                "source": resolutions[setting.key].source,
                "version": resolutions[setting.key].version,
                "global": (
                    by_scope[(settings_registry.GLOBAL, setting.key)].value
                    if (settings_registry.GLOBAL, setting.key) in by_scope
                    else None
                ),
                "tenant": (
                    by_scope[(settings_registry.TENANT, setting.key)].value
                    if (settings_registry.TENANT, setting.key) in by_scope
                    else None
                ),
            }
            for setting in settings_registry.REGISTRY
        ]
        return (
            jsonify(
                {
                    "tenant": tenant_key,
                    "count": len(entries),
                    "restart_required": [
                        entry["key"] for entry in entries if entry["restart_required"]
                    ],
                    "settings": entries,
                }
            ),
            200,
        )

    @app.put("/ai/settings")
    @require_admin_token
    def settings_update():
        """Validate the whole batch, then write it, version-checked and audited."""
        payload = request.get_json(silent=True) or {}
        tenant_key = str(payload.get("tenant") or registry.DEFAULT_TENANT_KEY)
        updates = payload.get("values")
        if not isinstance(updates, dict):
            return _admin_error("values must be an object", 400)
        try:
            resolved = settings_registry.apply(
                resolved_settings,
                updates,
                tenant_key=tenant_key,
                scope=str(payload.get("scope") or settings_registry.GLOBAL),
                actor=str(payload.get("actor") or ""),
                reason=str(payload.get("reason") or ""),
                expected_version=_optional_int(payload.get("expected_version")),
            )
        except settings_registry.SettingError as exc:
            status = 409 if exc.code == "version_conflict" else 422
            return jsonify(
                {"message": str(exc), "code": exc.code, "severity": "danger"}
            ), status
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(f"Could not save settings ({_error_class(exc)})", 503)
        return jsonify(
            {"count": len(resolved), "result": [row.to_public_dict() for row in resolved]}
        ), 200

    @app.get("/ai/settings/history")
    @require_admin_token
    def settings_history():
        """Who changed what, before and after — the record rollback reads."""
        try:
            entries = settings_registry.history(
                resolved_settings,
                key=request.args.get("key") or None,
                limit=int(request.args.get("limit") or 50),
            )
        except settings_registry.SettingError as exc:
            return jsonify(
                {"message": str(exc), "code": exc.code, "severity": "danger"}
            ), 422
        except (TypeError, ValueError):
            return _admin_error("limit must be an integer", 400)
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(f"Could not read history ({_error_class(exc)})", 503)
        return jsonify({"count": len(entries), "result": entries}), 200

    @app.post("/ai/settings/rollback/<audit_id>")
    @require_admin_token
    def settings_rollback(audit_id: str):
        """Reinstate a previous value as a new version, never by erasing history."""
        if not _is_uuid(audit_id):
            return _admin_error("Change not found", 404)
        payload = request.get_json(silent=True) or {}
        tenant_key = str(payload.get("tenant") or registry.DEFAULT_TENANT_KEY)
        try:
            resolved = settings_registry.rollback(
                resolved_settings,
                audit_id,
                tenant_key=tenant_key,
                actor=str(payload.get("actor") or ""),
                reason=str(payload.get("reason") or ""),
            )
        except settings_registry.SettingError as exc:
            status = 404 if exc.code == "audit_not_found" else 422
            return jsonify(
                {"message": str(exc), "code": exc.code, "severity": "danger"}
            ), status
        except Exception as exc:  # pragma: no cover - database failure path
            return _admin_error(f"Could not roll back ({_error_class(exc)})", 503)
        return jsonify(resolved.to_public_dict()), 200

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
            # The provider's own sentence ("prepayment credits are depleted")
            # explains what to do; the bare "http_402" alone reads like a
            # connector bug. Stored text is capped to last_test_error VARCHAR(255).
            detail = str(exc).strip()[:255]
            registry.record_test_result(
                resolved_settings,
                provider_id,
                status="failed",
                error=f"{exc.code}: {detail}"[:255] if detail else exc.code,
            )
            _record_health(provider_id, exc)
            _record_connection_test(
                record.name,
                record.default_model,
                correlation_id,
                duration_ms,
                error=exc,
            )
            logger.info(
                "provider_test correlation_id=%s provider=%s outcome=failed "
                "error_code=%s detail=%s duration_ms=%d",
                correlation_id,
                record.name,
                exc.code,
                detail or "-",
                duration_ms,
            )
            return (
                jsonify(
                    {
                        "status": "failed",
                        "error_code": exc.code,
                        "retryable": exc.retryable,
                        "message": detail,
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
        _record_connection_test(
            record.name, response.model, correlation_id, duration_ms, response=response
        )
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

