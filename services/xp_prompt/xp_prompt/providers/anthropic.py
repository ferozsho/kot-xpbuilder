"""Anthropic Messages API provider adapter."""

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

DEFAULT_ANTHROPIC_ENDPOINT = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"


class AnthropicProvider:
    """Anthropic ``/messages`` client with normalized usage reporting."""

    name = "anthropic"

    def __init__(
        self,
        *,
        credentials: ProviderCredentials,
        model: str,
        endpoint: str = DEFAULT_ANTHROPIC_ENDPOINT,
        timeout_seconds: int = 45,
        transport: JsonTransport | None = None,
        allow_local_http: bool = False,
    ) -> None:
        if not credentials.api_key.strip():
            raise ValueError("Anthropic API key is required")
        if not model.strip():
            raise ValueError("Anthropic model is required")
        self._credentials = credentials
        self.model = model.strip()
        self.endpoint = validate_provider_endpoint(
            endpoint, self.name, allow_local_http=allow_local_http
        )
        self.timeout_seconds = timeout_seconds
        self._transport = transport or UrlLibJsonTransport()

    def generate(self, request: ProviderRequest) -> ProviderResponse:
        system_parts = [m.content for m in request.messages if m.role == "system"]
        messages = [
            {"role": message.role, "content": message.content}
            for message in request.messages
            if message.role != "system"
        ]
        if not messages:
            raise ValueError("Anthropic request requires a user or assistant message")

        payload: dict[str, object] = {
            "model": self.model,
            "max_tokens": request.max_output_tokens,
            "temperature": request.temperature,
            "messages": messages,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if request.json_output:
            payload["system"] = (
                f"{payload.get('system', '')}\n\nReply with a single JSON object."
            ).strip()

        decoded, _ = self._transport.post_json(
            provider=self.name,
            url=f"{self.endpoint}/messages",
            headers={
                "x-api-key": self._credentials.api_key,
                "anthropic-version": ANTHROPIC_VERSION,
            },
            payload=payload,
            timeout_seconds=self.timeout_seconds,
        )
        return self._parse_response(decoded)

    def _parse_response(self, decoded: dict[str, object]) -> ProviderResponse:
        blocks = decoded.get("content")
        text = "".join(
            block.get("text", "")
            for block in blocks
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        ) if isinstance(blocks, list) else ""
        if not text.strip():
            raise ProviderError(
                provider=self.name,
                code="empty_response",
                message="Anthropic returned empty content",
                retryable=False,
            )

        usage = decoded.get("usage")
        usage_dict = usage if isinstance(usage, dict) else {}
        return ProviderResponse(
            provider=self.name,
            model=str(decoded.get("model") or self.model),
            content=text,
            finish_reason=(
                str(decoded.get("stop_reason"))
                if decoded.get("stop_reason") is not None
                else None
            ),
            request_id=(
                str(decoded.get("id")) if decoded.get("id") is not None else None
            ),
            usage=TokenUsage(
                input_tokens=_optional_int(usage_dict.get("input_tokens")),
                cached_input_tokens=_optional_int(
                    usage_dict.get("cache_read_input_tokens")
                ),
                output_tokens=_optional_int(usage_dict.get("output_tokens")),
                reasoning_tokens=None,
                total_tokens=None,
                source="reported" if usage_dict else "missing",
            ),
        )


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and value >= 0 else None
