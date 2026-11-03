"""Provider contract and fallback-routing tests."""

from __future__ import annotations

import unittest
from collections.abc import Mapping
from typing import Any

from xp_prompt.providers.deepseek import DeepSeekProvider
from xp_prompt.providers.gemini import GeminiProvider
from xp_prompt.providers.anthropic import AnthropicProvider
from xp_prompt.providers.openai_compatible import (
    AzureOpenAIProvider,
    OpenAICompatibleProvider,
)
from xp_prompt.providers.router import ProviderRouter
from xp_prompt.providers.types import (
    Message,
    ProviderCredentials,
    ProviderError,
    ProviderRequest,
    ProviderResponse,
    ProviderRoutingError,
    TokenUsage,
)


class FakeTransport:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def post_json(
        self,
        *,
        provider: str,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: int,
    ) -> tuple[dict[str, Any], Mapping[str, str]]:
        self.calls.append(
            {
                "provider": provider,
                "url": url,
                "headers": dict(headers),
                "payload": dict(payload),
                "timeout_seconds": timeout_seconds,
            }
        )
        return self.response, {}


class StubProvider:
    def __init__(
        self,
        name: str,
        model: str,
        result: ProviderResponse | ProviderError,
    ) -> None:
        self.name = name
        self.model = model
        self.result = result
        self.calls = 0

    def generate(self, request: ProviderRequest) -> ProviderResponse:
        del request
        self.calls += 1
        if isinstance(self.result, ProviderError):
            raise self.result
        return self.result


def request(*, json_output: bool = False) -> ProviderRequest:
    return ProviderRequest(
        operation="sql.generate",
        messages=(
            Message("system", "Return a safe query."),
            Message("user", "Show monthly enrollment totals."),
        ),
        max_output_tokens=512,
        temperature=0.0,
        json_output=json_output,
    )


def response(provider: str, model: str) -> ProviderResponse:
    return ProviderResponse(
        provider=provider,
        model=model,
        content="SELECT 1",
        finish_reason="stop",
        request_id="request-1",
        usage=TokenUsage(10, 0, 2, 0, 12, "reported"),
    )


class ProviderAdapterTest(unittest.TestCase):
    def test_provider_endpoints_require_https(self) -> None:
        with self.assertRaisesRegex(ValueError, "absolute HTTPS"):
            DeepSeekProvider(
                credentials=ProviderCredentials("secret-key"),
                model="deepseek-flash",
                endpoint="http://deepseek.invalid",
            )

    def test_gemini_request_and_usage_are_normalized(self) -> None:
        transport = FakeTransport(
            {
                "responseId": "gemini-response",
                "modelVersion": "gemini-test-001",
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {"parts": [{"text": '{"sql":"SELECT 1"}'}]},
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 20,
                    "cachedContentTokenCount": 5,
                    "candidatesTokenCount": 7,
                    "thoughtsTokenCount": 3,
                    "totalTokenCount": 30,
                },
            }
        )
        provider = GeminiProvider(
            credentials=ProviderCredentials("secret-key"),
            model="gemini-test",
            transport=transport,
        )

        result = provider.generate(request(json_output=True))

        self.assertEqual(result.request_id, "gemini-response")
        self.assertEqual(result.usage.cached_input_tokens, 5)
        self.assertEqual(result.usage.reasoning_tokens, 3)
        call = transport.calls[0]
        self.assertEqual(call["headers"]["x-goog-api-key"], "secret-key")
        self.assertNotIn("secret-key", call["url"])
        self.assertEqual(
            call["payload"]["generationConfig"]["responseMimeType"],
            "application/json",
        )

    def test_gemini_model_prefix_is_normalized(self) -> None:
        provider = GeminiProvider(
            credentials=ProviderCredentials("secret-key"),
            model="models/gemini-test",
            transport=FakeTransport({}),
        )

        self.assertEqual(provider.model, "gemini-test")

    def test_deepseek_request_and_usage_are_normalized(self) -> None:
        transport = FakeTransport(
            {
                "id": "deepseek-response",
                "model": "deepseek-flash",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "SELECT 1"},
                    }
                ],
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 8,
                    "total_tokens": 28,
                    "prompt_cache_hit_tokens": 6,
                    "prompt_cache_miss_tokens": 14,
                    "completion_tokens_details": {"reasoning_tokens": 3},
                },
            }
        )
        provider = DeepSeekProvider(
            credentials=ProviderCredentials("secret-key"),
            model="deepseek-flash",
            transport=transport,
        )

        result = provider.generate(request())

        self.assertEqual(result.request_id, "deepseek-response")
        self.assertEqual(result.usage.cached_input_tokens, 6)
        self.assertEqual(result.usage.reasoning_tokens, 3)
        call = transport.calls[0]
        self.assertEqual(call["headers"]["Authorization"], "Bearer secret-key")
        self.assertEqual(call["payload"]["stream"], False)

    def test_openai_compatible_uses_bearer_auth(self) -> None:
        transport = FakeTransport(
            {
                "id": "chatcmpl-1",
                "model": "gpt-4o-mini",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "OK"},
                    }
                ],
                "usage": {
                    "prompt_tokens": 5,
                    "completion_tokens": 1,
                    "total_tokens": 6,
                    "prompt_tokens_details": {"cached_tokens": 2},
                },
            }
        )
        provider = OpenAICompatibleProvider(
            credentials=ProviderCredentials("secret-key"),
            model="gpt-4o-mini",
            transport=transport,
        )

        result = provider.generate(request(json_output=True))

        self.assertEqual(result.request_id, "chatcmpl-1")
        self.assertEqual(result.usage.cached_input_tokens, 2)
        call = transport.calls[0]
        self.assertEqual(call["url"], "https://api.openai.com/v1/chat/completions")
        self.assertEqual(call["headers"]["Authorization"], "Bearer secret-key")
        self.assertEqual(call["payload"]["response_format"], {"type": "json_object"})

    def test_azure_openai_uses_deployment_url_and_api_key_header(self) -> None:
        transport = FakeTransport(
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "OK"},
                    }
                ]
            }
        )
        provider = AzureOpenAIProvider(
            credentials=ProviderCredentials("secret-key"),
            model="prod-deployment",
            endpoint="https://example.openai.azure.com",
            api_version="2024-10-21",
            transport=transport,
        )

        provider.generate(request())

        call = transport.calls[0]
        self.assertEqual(
            call["url"],
            "https://example.openai.azure.com/openai/deployments/prod-deployment/"
            "chat/completions?api-version=2024-10-21",
        )
        self.assertEqual(call["headers"]["api-key"], "secret-key")
        self.assertNotIn("Authorization", call["headers"])

    def test_anthropic_request_and_usage_are_normalized(self) -> None:
        transport = FakeTransport(
            {
                "id": "msg_1",
                "model": "claude-3-5-sonnet-latest",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "SELECT 1"}],
                "usage": {
                    "input_tokens": 12,
                    "output_tokens": 4,
                    "cache_read_input_tokens": 3,
                },
            }
        )
        provider = AnthropicProvider(
            credentials=ProviderCredentials("secret-key"),
            model="claude-3-5-sonnet-latest",
            transport=transport,
        )

        result = provider.generate(request())

        self.assertEqual(result.content, "SELECT 1")
        self.assertEqual(result.usage.cached_input_tokens, 3)
        call = transport.calls[0]
        self.assertEqual(call["url"], "https://api.anthropic.com/v1/messages")
        self.assertEqual(call["headers"]["x-api-key"], "secret-key")
        self.assertEqual(call["headers"]["anthropic-version"], "2023-06-01")
        self.assertNotIn("secret-key", str(call["payload"]))
        self.assertEqual(
            call["payload"]["system"], "Return a safe query."
        )
        self.assertEqual(len(call["payload"]["messages"]), 1)


class ProviderRouterTest(unittest.TestCase):
    def test_retryable_primary_failure_uses_one_fallback(self) -> None:
        primary = StubProvider(
            "gemini",
            "gemini-test",
            ProviderError(
                provider="gemini",
                code="http_503",
                message="provider unavailable",
                retryable=True,
                status_code=503,
            ),
        )
        fallback = StubProvider(
            "deepseek", "deepseek-flash", response("deepseek", "deepseek-flash")
        )

        routed = ProviderRouter(primary, fallback).generate(request())

        self.assertEqual(routed.response.provider, "deepseek")
        self.assertEqual([a.outcome for a in routed.attempts], ["failed", "succeeded"])
        self.assertEqual(primary.calls, 1)
        self.assertEqual(fallback.calls, 1)

    def test_nonretryable_failure_does_not_fallback(self) -> None:
        primary = StubProvider(
            "gemini",
            "gemini-test",
            ProviderError(
                provider="gemini",
                code="content_blocked",
                message="blocked",
                retryable=False,
            ),
        )
        fallback = StubProvider(
            "deepseek", "deepseek-flash", response("deepseek", "deepseek-flash")
        )

        with self.assertRaises(ProviderRoutingError) as raised:
            ProviderRouter(primary, fallback).generate(request())

        self.assertEqual(len(raised.exception.attempts), 1)
        self.assertEqual(fallback.calls, 0)

    def test_failed_fallback_retains_both_attempts(self) -> None:
        primary = StubProvider(
            "gemini",
            "gemini-test",
            ProviderError(
                provider="gemini",
                code="http_503",
                message="unavailable",
                retryable=True,
            ),
        )
        fallback = StubProvider(
            "deepseek",
            "deepseek-flash",
            ProviderError(
                provider="deepseek",
                code="http_429",
                message="rate limited",
                retryable=True,
            ),
        )

        with self.assertRaises(ProviderRoutingError) as raised:
            ProviderRouter(primary, fallback).generate(request())

        self.assertEqual(len(raised.exception.attempts), 2)
        self.assertEqual(raised.exception.attempts[-1].provider, "deepseek")


if __name__ == "__main__":
    unittest.main()

