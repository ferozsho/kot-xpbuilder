"""DeepSeek Chat Completions provider adapter."""

from __future__ import annotations

from xp_prompt.providers.transport import (
    JsonTransport,
    UrlLibJsonTransport,
    validate_provider_endpoint,
)
from xp_prompt.providers.types import (
    ProviderCredentials,
    ProviderError,
    ProviderRequest,
    ProviderResponse,
    TokenUsage,
)


class DeepSeekProvider:
    name = "deepseek"

    def __init__(
        self,
        *,
        credentials: ProviderCredentials,
        model: str,
        endpoint: str = "https://api.deepseek.com",
        timeout_seconds: int = 45,
        transport: JsonTransport | None = None,
        allow_local_http: bool = False,
    ) -> None:
        if not credentials.api_key.strip():
            raise ValueError("DeepSeek API key is required")
        if not model.strip():
            raise ValueError("DeepSeek model is required")
        self._credentials = credentials
        self.model = model.strip()
        self.endpoint = validate_provider_endpoint(
            endpoint, self.name, allow_local_http=allow_local_http
        )
        self.timeout_seconds = timeout_seconds
        self._transport = transport or UrlLibJsonTransport()

    def generate(self, request: ProviderRequest) -> ProviderResponse:
        payload: dict[str, object] = {
            "model": self.model,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in request.messages
            ],
            "max_tokens": request.max_output_tokens,
            "temperature": request.temperature,
            "stream": False,
        }
        if request.json_output:
            payload["response_format"] = {"type": "json_object"}

        decoded, _ = self._transport.post_json(
            provider=self.name,
            url=f"{self.endpoint}/chat/completions",
            headers={"Authorization": f"Bearer {self._credentials.api_key}"},
            payload=payload,
            timeout_seconds=self.timeout_seconds,
        )
        return self._parse_response(decoded)

    def _parse_response(self, decoded: dict[str, object]) -> ProviderResponse:
        choices = decoded.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ProviderError(
                provider=self.name,
                code="empty_response",
                message="DeepSeek returned no choice",
                retryable=False,
            )
        choice = choices[0]
        message = choice.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise ProviderError(
                provider=self.name,
                code="empty_response",
                message="DeepSeek returned empty content",
                retryable=False,
            )

        usage = decoded.get("usage")
        usage_dict = usage if isinstance(usage, dict) else {}
        prompt_details = usage_dict.get("prompt_tokens_details")
        completion_details = usage_dict.get("completion_tokens_details")
        prompt_detail_dict = prompt_details if isinstance(prompt_details, dict) else {}
        completion_detail_dict = (
            completion_details if isinstance(completion_details, dict) else {}
        )
        cached_tokens = usage_dict.get("prompt_cache_hit_tokens")
        if cached_tokens is None:
            cached_tokens = prompt_detail_dict.get("cached_tokens")

        return ProviderResponse(
            provider=self.name,
            model=str(decoded.get("model") or self.model),
            content=content,
            finish_reason=(
                str(choice.get("finish_reason"))
                if choice.get("finish_reason") is not None
                else None
            ),
            request_id=(
                str(decoded.get("id")) if decoded.get("id") is not None else None
            ),
            usage=TokenUsage(
                input_tokens=_optional_int(usage_dict.get("prompt_tokens")),
                cached_input_tokens=_optional_int(cached_tokens),
                output_tokens=_optional_int(usage_dict.get("completion_tokens")),
                reasoning_tokens=_optional_int(
                    completion_detail_dict.get("reasoning_tokens")
                ),
                total_tokens=_optional_int(usage_dict.get("total_tokens")),
                source="reported" if usage_dict else "missing",
            ),
        )


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and value >= 0 else None

