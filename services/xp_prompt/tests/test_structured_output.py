"""Structured-output validation tests (Phase 3, P3-F10).

A provider that was asked for JSON can still answer with prose, a Markdown fence,
a truncated document, or an array. Routing must treat all of those as a
non-retryable validation failure instead of handing raw model text upstream.
"""

from __future__ import annotations

import unittest

from xp_prompt.providers.router import ProviderRouter
from xp_prompt.providers.structured import (
    MAX_STRUCTURED_BYTES,
    StructuredOutputError,
    parse_json_object,
)
from xp_prompt.providers.types import (
    Message,
    ProviderError,
    ProviderRequest,
    ProviderResponse,
    ProviderRoutingError,
    TokenUsage,
)


def request(
    *,
    json_output: bool = True,
    json_required_keys: tuple[str, ...] = (),
) -> ProviderRequest:
    return ProviderRequest(
        operation="sql.generate",
        messages=(Message("user", "Return JSON."),),
        max_output_tokens=256,
        json_output=json_output,
        json_required_keys=json_required_keys,
    )


class StubProvider:
    def __init__(self, name: str, content: str) -> None:
        self.name = name
        self.model = "stub-model"
        self.content = content
        self.calls = 0

    def generate(self, request: ProviderRequest) -> ProviderResponse:
        del request
        self.calls += 1
        return ProviderResponse(
            provider=self.name,
            model=self.model,
            content=self.content,
            finish_reason="stop",
            request_id="req-1",
            usage=TokenUsage(1, 0, 1, 0, 2, "reported"),
        )


class ParseJsonObjectTest(unittest.TestCase):
    def test_plain_object_is_parsed(self) -> None:
        self.assertEqual(parse_json_object('{"sql": "SELECT 1"}'), {"sql": "SELECT 1"})

    def test_markdown_fence_is_unwrapped(self) -> None:
        fenced = '```json\n{"sql": "SELECT 1"}\n```'
        self.assertEqual(parse_json_object(fenced), {"sql": "SELECT 1"})

    def test_required_keys_are_enforced(self) -> None:
        with self.assertRaises(StructuredOutputError) as raised:
            parse_json_object('{"sql": "SELECT 1"}', required_keys=("sql", "notes"))

        self.assertEqual(raised.exception.code, "structured_output_invalid")
        self.assertIn("notes", str(raised.exception))

    def test_array_is_rejected(self) -> None:
        with self.assertRaises(StructuredOutputError):
            parse_json_object('[{"sql": "SELECT 1"}]')

    def test_scalar_is_rejected(self) -> None:
        with self.assertRaises(StructuredOutputError):
            parse_json_object('"SELECT 1"')

    def test_truncated_json_is_rejected(self) -> None:
        with self.assertRaises(StructuredOutputError) as raised:
            parse_json_object('{"sql": "SELECT 1"')

        self.assertIn("not valid JSON", str(raised.exception))

    def test_prose_is_rejected(self) -> None:
        with self.assertRaises(StructuredOutputError):
            parse_json_object("Sure! Here is your query: SELECT 1")

    def test_empty_content_is_rejected(self) -> None:
        with self.assertRaises(StructuredOutputError):
            parse_json_object("   ")

    def test_oversized_content_is_rejected(self) -> None:
        oversized = '{"sql": "' + ("x" * MAX_STRUCTURED_BYTES) + '"}'
        with self.assertRaises(StructuredOutputError) as raised:
            parse_json_object(oversized)

        self.assertIn("too large", str(raised.exception))


class ProviderRequestValidationTest(unittest.TestCase):
    def test_required_keys_need_json_output(self) -> None:
        with self.assertRaises(ValueError):
            request(json_output=False, json_required_keys=("sql",))

    def test_blank_required_key_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            request(json_required_keys=(" ",))

    def test_required_key_count_is_bounded(self) -> None:
        with self.assertRaises(ValueError):
            request(json_required_keys=tuple(f"k{index}" for index in range(33)))


class RouterStructuredOutputTest(unittest.TestCase):
    @staticmethod
    def router(primary: StubProvider, fallback: StubProvider | None = None):
        return ProviderRouter(
            primary,
            fallback,
            sleep=lambda _seconds: None,
            jitter=lambda: 0.5,
        )

    def test_valid_json_output_passes_through(self) -> None:
        provider = StubProvider("gemini", '{"sql": "SELECT 1"}')

        routed = self.router(provider).generate(request())

        self.assertEqual(routed.response.content, '{"sql": "SELECT 1"}')
        self.assertEqual(provider.calls, 1)

    def test_prose_blocks_the_request_without_retry_or_fallback(self) -> None:
        primary = StubProvider("gemini", "Here you go: SELECT 1")
        fallback = StubProvider("deepseek", '{"sql": "SELECT 2"}')

        with self.assertRaises(ProviderRoutingError) as raised:
            self.router(primary, fallback).generate(request())

        self.assertEqual(raised.exception.code, "structured_output_invalid")
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(len(raised.exception.attempts), 1)
        self.assertEqual(primary.calls, 1)
        self.assertEqual(fallback.calls, 0)

    def test_validation_is_skipped_for_plain_text_requests(self) -> None:
        provider = StubProvider("gemini", "Here you go: SELECT 1")

        routed = self.router(provider).generate(request(json_output=False))

        self.assertEqual(routed.response.content, "Here you go: SELECT 1")

    def test_validation_error_does_not_leak_model_text(self) -> None:
        provider = StubProvider("gemini", "SECRET-MODEL-OUTPUT")

        with self.assertRaises(ProviderRoutingError) as raised:
            self.router(provider).generate(request())

        self.assertNotIn("SECRET-MODEL-OUTPUT", str(raised.exception))
        self.assertNotIn("SECRET-MODEL-OUTPUT", repr(raised.exception))

    def test_provider_errors_still_use_the_router_path(self) -> None:
        class Failing(StubProvider):
            def generate(self, request: ProviderRequest) -> ProviderResponse:
                raise ProviderError(
                    provider="gemini",
                    code="http_400",
                    message="bad request",
                    retryable=False,
                )

        with self.assertRaises(ProviderRoutingError) as raised:
            self.router(Failing("gemini", "")).generate(request())

        self.assertEqual(raised.exception.code, "http_400")

    def test_missing_required_key_blocks_the_request(self) -> None:
        provider = StubProvider("gemini", '{"rationale": "counts rows"}')

        with self.assertRaises(ProviderRoutingError) as raised:
            self.router(provider).generate(
                request(json_required_keys=("sql", "rationale"))
            )

        self.assertEqual(raised.exception.code, "structured_output_invalid")
        self.assertFalse(raised.exception.retryable)
        self.assertIn("sql", str(raised.exception))

    def test_all_required_keys_present_passes_through(self) -> None:
        provider = StubProvider(
            "gemini", '{"sql": "SELECT 1", "rationale": "counts rows"}'
        )

        routed = self.router(provider).generate(
            request(json_required_keys=("sql", "rationale"))
        )

        self.assertIn("SELECT 1", routed.response.content)


if __name__ == "__main__":
    unittest.main()
