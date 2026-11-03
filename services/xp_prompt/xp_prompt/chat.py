"""The conversational turn: persist the question, call a provider, persist the answer.

Phase 2 (P2-F02) needs one place where a chat turn happens, so the workspace, a
future job runner, and the tests all behave identically. A turn is deliberately
boring and auditable:

1. the user message is stored first, so a provider failure never loses input;
2. history is read back from the database (sealed bodies), not from the client;
3. a single correlated provider call goes through
   :func:`xp_prompt.providers.registry.resolve_router`, which already applies
   routing, retries, the circuit breaker, and structured-output validation;
4. the answer is stored with its correlation id, provider, model, and usage so a
   later usage ledger (Phase 9) can reconcile without re-reading the transcript.

No prompt or completion is logged; only the correlation id, provider, model, and
token counts, which are already normalized and free of user content.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from xp_prompt import conversations
from xp_prompt.config import Settings
from xp_prompt.providers import registry
from xp_prompt.providers.types import (
    Message,
    ProviderError,
    ProviderRequest,
)

logger = logging.getLogger(__name__)

#: How many prior messages are replayed as context. Older turns stay stored but
#: are not resent, so a long chat cannot grow the prompt without bound.
MAX_HISTORY_MESSAGES = 20
#: Chat answers are short; a larger budget only buys latency and cost.
CHAT_MAX_OUTPUT_TOKENS = 1024
CHAT_TEMPERATURE = 0.2
#: Hard limit for one turn: the whole prompt is dropped, not truncated, when the
#: user message alone exceeds this.
MAX_PROMPT_CHARS = 8_000

SYSTEM_PROMPT = (
    "You are XP Prompt, the analytics assistant inside XPBuilder (a Superset "
    "deployment). Answer in plain language, keep answers short, and state any "
    "assumption you make. Never invent query results you have not been given. "
    "If the request is ambiguous, ask one focused clarifying question instead "
    "of guessing."
)

CHAT_FAILED_CODE = "chat_provider_unavailable"


class ChatError(RuntimeError):
    """A turn that could not be completed; ``code`` is stable and content-free."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ChatTurn:
    """Result of one user turn."""

    conversation_id: str
    correlation_id: str
    answer: str
    provider: str
    model: str
    usage: dict[str, Any] = field(default_factory=dict)
    attempts: tuple[dict[str, Any], ...] = ()


def run_turn(
    settings: Settings,
    *,
    conversation_id: str,
    tenant_key: str,
    superset_user_id: int,
    content: str,
) -> ChatTurn:
    """Persist the question, answer it with the routed provider, persist the answer."""
    conversation = conversations.get_conversation(
        settings,
        conversation_id,
        tenant_key=tenant_key,
        superset_user_id=superset_user_id,
    )
    if conversation is None:
        raise conversations.ConversationNotFound("conversation not found")

    user_message = conversations.append_message(
        settings,
        conversation_id,
        tenant_key=tenant_key,
        superset_user_id=superset_user_id,
        role="user",
        content=content,
    )

    history = conversations.list_messages(
        settings,
        conversation_id,
        tenant_key=tenant_key,
        superset_user_id=superset_user_id,
        limit=MAX_HISTORY_MESSAGES,
    )
    request = _build_request(history, user_message.id)

    try:
        router = registry.resolve_router(settings, tenant_key=tenant_key)
    except registry.ProviderConfigError as exc:
        raise ChatError("no_provider", str(exc)) from exc

    try:
        routed = router.generate(request)
    except ProviderError as exc:
        logger.info(
            "chat_turn correlation_id=%s conversation=%s outcome=failed error_code=%s",
            request.correlation_id,
            conversation_id,
            exc.code,
        )
        conversations.append_message(
            settings,
            conversation_id,
            tenant_key=tenant_key,
            superset_user_id=superset_user_id,
            role="assistant",
            content=(
                "I could not reach the AI provider for that answer "
                f"(error: {exc.code}). Nothing was saved from the attempt."
            ),
            structured_content={
                "kind": "error",
                "error_code": exc.code,
                "retryable": exc.retryable,
                "correlation_id": request.correlation_id,
            },
        )
        raise ChatError(exc.code, "The AI provider could not complete the request") from exc

    scored_correlation = routed.attempts[0].correlation_id if routed.attempts else None
    correlation_id = scored_correlation or request.correlation_id or ""
    usage = _usage_dict(routed.response.usage)
    attempts = tuple(
        {
            "provider": attempt.provider,
            "model": attempt.model,
            "outcome": attempt.outcome,
            "attempt": attempt.attempt,
            "error_code": attempt.error_code,
        }
        for attempt in routed.attempts
    )

    conversations.append_message(
        settings,
        conversation_id,
        tenant_key=tenant_key,
        superset_user_id=superset_user_id,
        role="assistant",
        content=routed.response.content,
        structured_content={
            "kind": "answer",
            "correlation_id": correlation_id,
            "provider": routed.response.provider,
            "model": routed.response.model,
            "finish_reason": routed.response.finish_reason,
            "usage": usage,
            "attempts": list(attempts),
        },
    )

    logger.info(
        "chat_turn correlation_id=%s conversation=%s provider=%s model=%s "
        "attempts=%d outcome=succeeded",
        correlation_id,
        conversation_id,
        routed.response.provider,
        routed.response.model,
        len(routed.attempts),
    )

    return ChatTurn(
        conversation_id=conversation_id,
        correlation_id=correlation_id,
        answer=routed.response.content,
        provider=routed.response.provider,
        model=routed.response.model,
        usage=usage,
        attempts=attempts,
    )


def _build_request(
    history: list["conversations.MessageRecord"],
    last_user_message_id: str,
) -> ProviderRequest:
    """Turn stored history into one provider request.

    The budget is spent newest-first so the current turn is always included;
    messages whose body could not be decrypted (rotated secret) are skipped
    rather than sent as empty turns.
    """
    usable = [
        record
        for record in history
        if record.content
        and record.role in {"user", "assistant"}
        and record.content.strip()
    ]

    selected: list[conversations.MessageRecord] = []
    total_chars = 0
    for record in reversed(usable):
        text = record.content.strip()
        is_current_turn = record.id == last_user_message_id
        if not is_current_turn and total_chars + len(text) > MAX_PROMPT_CHARS:
            break
        selected.append(record)
        total_chars += len(text)

    if not any(record.id == last_user_message_id for record in selected):
        # The current turn is too large to send as part of the budget; it still
        # has to reach the model, so drop older context instead of the question.
        current = [r for r in usable if r.id == last_user_message_id]
        selected = current or selected

    messages: list[Message] = [Message("system", SYSTEM_PROMPT)]
    messages.extend(
        Message(record.role, record.content.strip())
        for record in reversed(selected)
    )
    if len(messages) == 1:
        raise conversations.ConversationInputError("Message content is required")
    return ProviderRequest(
        operation="chat.turn",
        messages=tuple(messages),
        max_output_tokens=CHAT_MAX_OUTPUT_TOKENS,
        temperature=CHAT_TEMPERATURE,
        correlation_id=uuid.uuid4().hex,
    )


def _usage_dict(usage: Any) -> dict[str, Any]:
    return {
        "input_tokens": usage.input_tokens,
        "cached_input_tokens": usage.cached_input_tokens,
        "output_tokens": usage.output_tokens,
        "reasoning_tokens": usage.reasoning_tokens,
        "total_tokens": usage.total_tokens,
        "source": usage.source,
    }
