"""Tests for the conversation store and the chat turn (P2-F02)."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest import mock

from xp_prompt import chat, conversations, intents
from xp_prompt.config import Settings
from xp_prompt.providers.types import (
    AttemptSummary,
    ProviderError,
    ProviderResponse,
    ProviderRoutingError,
    RoutedResponse,
    TokenUsage,
)

ROOT_SECRET = "unit-test-root-secret-value"


def settings() -> Settings:
    return Settings.from_env(
        {
            "XP_PROMPT_POSTGRES_HOST": "xp-prompt-db",
            "XP_PROMPT_POSTGRES_PORT": "5432",
            "XP_PROMPT_POSTGRES_USER": "xprompt",
            "XP_PROMPT_POSTGRES_PASSWORD": "database-secret",
            "XP_PROMPT_POSTGRES_DB": "xprompt",
            "XP_PROMPT_REDIS_HOST": "superset-redis",
            "XP_PROMPT_REDIS_PORT": "6379",
            "XP_PROMPT_REDIS_PASSWORD": "redis-secret",
            "XP_PROMPT_REDIS_DB": "2",
            "XP_PROMPT_QDRANT_URL": "http://xp-prompt-qdrant:6333",
            "XP_PROMPT_SERVICE_SECRET": ROOT_SECRET,
        }
    )


def message(
    *,
    id: str = "m-1",
    role: str = "user",
    content: str | None = "hello",
) -> conversations.MessageRecord:
    return conversations.MessageRecord(
        id=id,
        conversation_id="c-1",
        role=role,
        content=content,
        structured_content={},
        created_at=datetime(2026, 11, 3, tzinfo=timezone.utc),
    )


class RedactTest(unittest.TestCase):
    def test_api_key_shape_is_removed(self) -> None:
        self.assertNotIn("AIzaSyABCDEF1234567890", conversations.redact("key AIzaSyABCDEF1234567890"))

    def test_assignment_of_a_secret_is_removed(self) -> None:
        redacted = conversations.redact("password=hunter2 and api_key: abc123")

        self.assertNotIn("hunter2", redacted)
        self.assertNotIn("abc123", redacted)

    def test_long_text_is_truncated(self) -> None:
        redacted = conversations.redact("x" * 1000)

        self.assertLessEqual(len(redacted), conversations.MAX_EXCERPT_CHARS)
        self.assertTrue(redacted.endswith("…"))

    def test_whitespace_is_collapsed(self) -> None:
        self.assertEqual(conversations.redact("a\n\n  b\tc"), "a b c")


class ValidationTest(unittest.TestCase):
    def test_blank_message_is_rejected_before_any_database_work(self) -> None:
        with self.assertRaises(conversations.ConversationInputError):
            conversations.append_message(
                settings(),
                "c-1",
                tenant_key="default",
                superset_user_id=1,
                role="user",
                content="   ",
            )

    def test_oversized_message_is_rejected(self) -> None:
        with self.assertRaises(conversations.ConversationInputError):
            conversations.append_message(
                settings(),
                "c-1",
                tenant_key="default",
                superset_user_id=1,
                role="user",
                content="x" * (conversations.MAX_MESSAGE_CHARS + 1),
            )

    def test_unknown_role_is_rejected(self) -> None:
        with self.assertRaises(conversations.ConversationInputError):
            conversations.append_message(
                settings(),
                "c-1",
                tenant_key="default",
                superset_user_id=1,
                role="root",
                content="hello",
            )

    def test_owner_must_be_positive(self) -> None:
        with self.assertRaises(conversations.ConversationInputError):
            conversations.list_conversations(
                settings(), tenant_key="default", superset_user_id=0
            )

    def test_title_length_is_bounded(self) -> None:
        with self.assertRaises(conversations.ConversationInputError):
            conversations.create_conversation(
                settings(),
                tenant_key="default",
                superset_user_id=1,
                title="t" * (conversations.MAX_TITLE_CHARS + 1),
            )


class BuildRequestTest(unittest.TestCase):
    def test_current_turn_is_included_once(self) -> None:
        request = chat._build_request(
            [message(id="m-1"), message(id="m-2", role="assistant", content="hi")],
            "m-1",
        )

        roles = [item.role for item in request.messages]
        self.assertEqual(roles, ["system", "user", "assistant"])
        self.assertEqual(request.operation, "chat.turn")
        self.assertIsNotNone(request.correlation_id)

    def test_undecryptable_history_is_skipped(self) -> None:
        request = chat._build_request(
            [message(id="m-1", content="old"), message(id="m-1", content=None)],
            "m-2",
        )

        self.assertEqual([item.role for item in request.messages], ["system", "user"])

    def test_history_is_dropped_oldest_first_when_over_budget(self) -> None:
        history = [
            message(id="m-1", content="a" * 4000),
            message(id="m-2", content="b" * 4000),
            message(id="m-3", content="current"),
        ]

        request = chat._build_request(history, "m-3")

        contents = [item.content for item in request.messages]
        self.assertIn("current", contents)
        self.assertNotIn("a" * 4000, contents)

    def test_prompt_with_only_the_system_message_is_rejected(self) -> None:
        with self.assertRaises(conversations.ConversationInputError):
            chat._build_request([], "m-1")


class RunTurnTest(unittest.TestCase):
    def routed(self) -> RoutedResponse:
        return RoutedResponse(
            response=ProviderResponse(
                provider="gemini",
                model="gemini-2.5-flash",
                content="Here is the answer.",
                finish_reason="stop",
                request_id="req-1",
                usage=TokenUsage(10, 0, 5, 0, 15, "reported"),
            ),
            attempts=(
                AttemptSummary(
                    provider="gemini",
                    model="gemini-2.5-flash",
                    outcome="succeeded",
                    attempt=1,
                    correlation_id="corr-1",
                    operation="chat.turn",
                ),
            ),
        )

    def test_successful_turn_persists_both_messages(self) -> None:
        stored: list[dict] = []
        router = mock.Mock()
        router.generate.return_value = self.routed()

        with mock.patch.object(conversations, "get_conversation", return_value=object()), \
             mock.patch.object(
                 conversations,
                 "append_message",
                 side_effect=lambda *args, **kwargs: stored.append(kwargs) or message(),
             ), \
             mock.patch.object(
                 conversations,
                 "list_messages",
                 return_value=[message(id="m-1")],
             ), \
             mock.patch.object(chat.registry, "resolve_router", return_value=router):
            turn = chat.run_turn(
                settings(),
                conversation_id="c-1",
                tenant_key="default",
                superset_user_id=1,
                content="show monthly enrollment trends",
            )

        self.assertEqual([entry["role"] for entry in stored], ["user", "assistant"])
        self.assertEqual(turn.correlation_id, "corr-1")
        self.assertEqual(turn.answer, "Here is the answer.")
        self.assertEqual(turn.usage["total_tokens"], 15)
        structure = stored[1]["structured_content"]
        self.assertEqual(structure["correlation_id"], "corr-1")
        self.assertEqual(structure["kind"], "answer")
        self.assertEqual(structure["intent"], "query")
        self.assertEqual(turn.intent, "query")
        self.assertFalse(turn.clarification)

    def test_provider_failure_is_stored_then_raised(self) -> None:
        stored: list[dict] = []
        router = mock.Mock()
        router.generate.side_effect = ProviderRoutingError(
            cause=ProviderError(
                provider="gemini",
                code="http_503",
                message="unavailable",
                retryable=True,
            ),
            attempts=(),
            correlation_id="corr-2",
        )

        with mock.patch.object(conversations, "get_conversation", return_value=object()), \
             mock.patch.object(
                 conversations,
                 "append_message",
                 side_effect=lambda *args, **kwargs: stored.append(kwargs) or message(),
             ), \
             mock.patch.object(
                 conversations,
                 "list_messages",
                 return_value=[message(id="m-1")],
             ), \
             mock.patch.object(chat.registry, "resolve_router", return_value=router):
            with self.assertRaises(chat.ChatError) as raised:
                chat.run_turn(
                    settings(),
                    conversation_id="c-1",
                    tenant_key="default",
                    superset_user_id=1,
                    content="how many learners are active",
                )

        self.assertEqual(raised.exception.code, "http_503")
        self.assertEqual([entry["role"] for entry in stored], ["user", "assistant"])
        self.assertEqual(stored[1]["structured_content"]["kind"], "error")

    def test_missing_provider_is_reported_as_no_provider(self) -> None:
        with mock.patch.object(conversations, "get_conversation", return_value=object()), \
             mock.patch.object(conversations, "append_message", return_value=message()), \
             mock.patch.object(conversations, "list_messages", return_value=[message()]), \
             mock.patch.object(
                 chat.registry,
                 "resolve_router",
                 side_effect=chat.registry.ProviderConfigError("no provider"),
             ):
            with self.assertRaises(chat.ChatError) as raised:
                chat.run_turn(
                    settings(),
                    conversation_id="c-1",
                    tenant_key="default",
                    superset_user_id=1,
                    content="how many learners are active",
                )

        self.assertEqual(raised.exception.code, "no_provider")

    def test_unknown_conversation_is_not_created(self) -> None:
        append = mock.Mock()
        with mock.patch.object(conversations, "get_conversation", return_value=None), \
             mock.patch.object(conversations, "append_message", append):
            with self.assertRaises(conversations.ConversationNotFound):
                chat.run_turn(
                    settings(),
                    conversation_id="c-404",
                    tenant_key="default",
                    superset_user_id=1,
                    content="hello",
                )

        append.assert_not_called()

    def test_ambiguous_request_asks_a_question_without_a_provider_call(self) -> None:
        stored: list[dict] = []
        resolve = mock.Mock()

        with mock.patch.object(conversations, "get_conversation", return_value=object()), \
             mock.patch.object(
                 conversations,
                 "append_message",
                 side_effect=lambda *args, **kwargs: stored.append(kwargs) or message(),
             ), \
             mock.patch.object(
                 conversations,
                 "list_messages",
                 return_value=[message(id="m-1")],
             ), \
             mock.patch.object(chat.registry, "resolve_router", resolve):
            turn = chat.run_turn(
                settings(),
                conversation_id="c-1",
                tenant_key="default",
                superset_user_id=1,
                content="hello",
            )

        resolve.assert_not_called()
        self.assertTrue(turn.clarification)
        self.assertEqual(turn.intent, "clarification")
        self.assertEqual(turn.provider, "")
        self.assertEqual(turn.answer, intents.NO_SIGNAL_QUESTION)
        self.assertEqual([entry["role"] for entry in stored], ["user", "assistant"])
        self.assertEqual(stored[1]["content"], intents.NO_SIGNAL_QUESTION)
        self.assertEqual(stored[1]["structured_content"]["kind"], "clarification")

    def test_modification_without_history_parks_the_target_intent(self) -> None:
        stored: list[dict] = []
        resolve = mock.Mock()

        with mock.patch.object(conversations, "get_conversation", return_value=object()), \
             mock.patch.object(
                 conversations,
                 "append_message",
                 side_effect=lambda *args, **kwargs: stored.append(kwargs) or message(),
             ), \
             mock.patch.object(
                 conversations,
                 "list_messages",
                 return_value=[message(id="m-1", content="make it a line chart")],
             ), \
             mock.patch.object(chat.registry, "resolve_router", resolve):
            turn = chat.run_turn(
                settings(),
                conversation_id="c-1",
                tenant_key="default",
                superset_user_id=1,
                content="make it a line chart",
            )

        resolve.assert_not_called()
        self.assertTrue(turn.clarification)
        self.assertEqual(turn.target_intent, "chart")
        structure = stored[1]["structured_content"]
        self.assertEqual(structure["kind"], "clarification")
        self.assertEqual(structure["pending_intent"], "chart")

    def test_follow_up_reply_reuses_the_parked_intent(self) -> None:
        stored: list[dict] = []
        router = mock.Mock()
        router.generate.return_value = self.routed()
        clarification = conversations.MessageRecord(
            id="m-2",
            conversation_id="c-1",
            role="assistant",
            content=intents.NO_CONTEXT_QUESTION.format(target="a chart"),
            structured_content={
                "kind": "clarification",
                "pending_intent": "chart",
            },
            created_at=datetime(2026, 11, 3, tzinfo=timezone.utc),
        )

        with mock.patch.object(conversations, "get_conversation", return_value=object()), \
             mock.patch.object(
                 conversations,
                 "append_message",
                 side_effect=lambda *args, **kwargs: stored.append(kwargs) or message(),
             ), \
             mock.patch.object(
                 conversations,
                 "list_messages",
                 return_value=[clarification, message(id="m-3", content="yes")],
             ), \
             mock.patch.object(chat.registry, "resolve_router", return_value=router):
            turn = chat.run_turn(
                settings(),
                conversation_id="c-1",
                tenant_key="default",
                superset_user_id=1,
                content="yes",
            )

        self.assertFalse(turn.clarification)
        self.assertEqual(turn.intent, "chart")
        system_prompt = router.generate.call_args[0][0].messages[0].content
        self.assertIn("chart", system_prompt)


if __name__ == "__main__":
    unittest.main()
