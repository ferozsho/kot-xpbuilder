"""The conversational turn: persist the question, call a provider, persist the answer.

Phase 2 (P2-F02) needs one place where a chat turn happens, so the workspace, a
future job runner, and the tests all behave identically. A turn is deliberately
boring and auditable:

1. the user message is stored first, so a provider failure never loses input;
2. history is read back from the database (sealed bodies), not from the client;
3. :mod:`xp_prompt.intents` routes the turn to an intent before any provider
   cost is spent, and an unclear request is answered with a focused question
   instead of a guess;
4. a single correlated provider call goes through
   :func:`xp_prompt.providers.registry.resolve_router`, which already applies
   routing, retries, the circuit breaker, and structured-output validation;
5. the answer is stored with its correlation id, provider, model, intent, and
   usage so a later usage ledger (Phase 9) can reconcile without re-reading the
   transcript.

No prompt or completion is logged; only the correlation id, provider, model, and
token counts, which are already normalized and free of user content.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from xp_prompt import conversations, intents, responses
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
    """Result of one user turn.

    ``provider``/``model`` are empty on a clarification turn: the router answered
    from its own routing table, so no provider was called and no tokens were
    spent.
    """

    conversation_id: str
    correlation_id: str
    answer: str
    provider: str
    model: str
    intent: str
    clarification: bool = False
    target_intent: str | None = None
    intent_reasons: tuple[str, ...] = ()
    usage: dict[str, Any] = field(default_factory=dict)
    attempts: tuple[dict[str, Any], ...] = ()


def run_turn(
    settings: Settings,
    *,
    conversation_id: str,
    tenant_key: str,
    superset_user_id: int,
    content: str,
    expected_context_version: int | None = None,
) -> ChatTurn:
    """Persist the question, answer it with the routed provider, persist the answer.

    ``expected_context_version`` is the version the caller prepared against. A
    mismatch means the context was reset (or replaced) since the client loaded
    the conversation, so the turn is rejected rather than answered from a
    history the caller has not seen.
    """
    conversation = conversations.get_conversation(
        settings,
        conversation_id,
        tenant_key=tenant_key,
        superset_user_id=superset_user_id,
    )
    if conversation is None:
        raise conversations.ConversationNotFound("conversation not found")
    current_version = _current_context_version(conversation)
    if (
        expected_context_version is not None
        and int(expected_context_version) != current_version
    ):
        raise conversations.ContextVersionConflict(
            "conversation context changed; reload the conversation"
        )

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
        context_version=current_version,
    )

    has_prior_answer = _has_prior_answer(history, user_message.id)

    decision = intents.classify(
        content,
        has_prior_answer=has_prior_answer,
        pending_intent=_pending_intent(history, user_message.id),
    )
    if decision.is_clarification:
        return _clarification_turn(
            settings,
            conversation_id=conversation_id,
            tenant_key=tenant_key,
            superset_user_id=superset_user_id,
            decision=decision,
        )

    request = _build_request(history, user_message.id, intent=decision.intent)

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
            "intent": decision.intent,
            "target_intent": decision.target_intent,
            "intent_reasons": list(decision.reasons),
            "intent_confidence": decision.confidence,
            "blocks": responses.answer_blocks(
                decision.intent,
                content=content,
                has_prior_answer=has_prior_answer,
            ),
            "correlation_id": correlation_id,
            "provider": routed.response.provider,
            "model": routed.response.model,
            "finish_reason": routed.response.finish_reason,
            "usage": usage,
            "attempts": list(attempts),
        },
    )

    logger.info(
        "chat_turn correlation_id=%s conversation=%s intent=%s provider=%s "
        "model=%s attempts=%d outcome=succeeded",
        correlation_id,
        conversation_id,
        decision.intent,
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
        intent=decision.intent,
        target_intent=decision.target_intent,
        intent_reasons=decision.reasons,
        usage=usage,
        attempts=attempts,
    )


def _clarification_turn(
    settings: Settings,
    *,
    conversation_id: str,
    tenant_key: str,
    superset_user_id: int,
    decision: intents.IntentDecision,
) -> ChatTurn:
    """Store the router's question and return it without spending a token.

    The question is the assistant's answer for this turn. It carries the
    ``pending_intent`` so a one-word reply continues where the router left off.
    """
    correlation_id = uuid.uuid4().hex
    question = decision.clarifying_question or intents.NO_SIGNAL_QUESTION

    conversations.append_message(
        settings,
        conversation_id,
        tenant_key=tenant_key,
        superset_user_id=superset_user_id,
        role="assistant",
        content=question,
        structured_content={
            "kind": "clarification",
            "intent": intents.CLARIFICATION,
            "pending_intent": decision.pending_intent,
            "target_intent": decision.target_intent,
            "intent_reasons": list(decision.reasons),
            "blocks": responses.clarification_blocks(question),
            "correlation_id": correlation_id,
        },
    )

    logger.info(
        "chat_turn correlation_id=%s conversation=%s intent=clarification "
        "reasons=%s pending_intent=%s outcome=clarification",
        correlation_id,
        conversation_id,
        ",".join(decision.reasons) or "none",
        decision.pending_intent or "none",
    )

    return ChatTurn(
        conversation_id=conversation_id,
        correlation_id=correlation_id,
        answer=question,
        provider="",
        model="",
        intent=intents.CLARIFICATION,
        clarification=True,
        target_intent=decision.target_intent,
        intent_reasons=decision.reasons,
    )


def _current_context_version(conversation: Any) -> int:
    """Context generation of a loaded conversation, ``1`` when it carries none.

    A store record always has the column; the fallback only keeps a partially
    constructed record (a test double, or a future read model) usable.
    """
    return int(getattr(conversation, "context_version", 1) or 1)


def _has_prior_answer(
    history: list["conversations.MessageRecord"],
    current_user_message_id: str,
) -> bool:
    """Does this conversation already hold an assistant turn to modify?

    A stored error or a clarification question is not material anyone can
    change: only a real answer makes "make it a line chart" meaningful. Live
    verification caught this — after one ambiguous message, "make it a line
    chart" was routed to a modification of the router's own question.
    """
    for record in history:
        if record.id == current_user_message_id:
            continue
        if record.role != "assistant" or not (record.content or "").strip():
            continue
        if record.structured_content.get("kind") in {"error", "clarification"}:
            continue
        return True
    return False


def _pending_intent(
    history: list["conversations.MessageRecord"],
    current_user_message_id: str,
) -> str | None:
    """Intent carried by the last clarification, if that was the last answer."""
    for record in reversed(history):
        if record.id == current_user_message_id:
            continue
        if record.role != "assistant":
            continue
        if record.structured_content.get("kind") != "clarification":
            return None
        pending = record.structured_content.get("pending_intent")
        return pending if isinstance(pending, str) else None
    return None


def _build_request(
    history: list["conversations.MessageRecord"],
    last_user_message_id: str,
    *,
    intent: str | None = None,
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

    messages: list[Message] = [Message("system", _system_prompt(intent))]
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


def _system_prompt(intent: str | None) -> str:
    """Base prompt plus the guidance the routed intent implies."""
    guidance = intents.PROMPT_GUIDANCE.get(intent or "")
    return f"{SYSTEM_PROMPT}\n\n{guidance}" if guidance else SYSTEM_PROMPT


def _usage_dict(usage: Any) -> dict[str, Any]:
    return {
        "input_tokens": usage.input_tokens,
        "cached_input_tokens": usage.cached_input_tokens,
        "output_tokens": usage.output_tokens,
        "reasoning_tokens": usage.reasoning_tokens,
        "total_tokens": usage.total_tokens,
        "source": usage.source,
    }
