"""Google Gemini generateContent provider adapter."""

from __future__ import annotations

from urllib.parse import quote

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


class GeminiProvider:
    name = "gemini"

    def __init__(
        self,
        *,
        credentials: ProviderCredentials,
        model: str,
        endpoint: str = "https://generativelanguage.googleapis.com/v1beta",
        timeout_seconds: int = 45,
        transport: JsonTransport | None = None,
        allow_local_http: bool = False,
    ) -> None:
        if not credentials.api_key.strip():
            raise ValueError("Gemini API key is required")
        if not model.strip():
            raise ValueError("Gemini model is required")
        self._credentials = credentials
        normalized_model = model.strip()
        self.model = (
            normalized_model.removeprefix("models/")
            if normalized_model.startswith("models/")
            else normalized_model
        )
        self.endpoint = validate_provider_endpoint(
            endpoint, self.name, allow_local_http=allow_local_http
        )
        self.timeout_seconds = timeout_seconds
        self._transport = transport or UrlLibJsonTransport()

    def generate(self, request: ProviderRequest) -> ProviderResponse:
        system_parts = [m.content for m in request.messages if m.role == "system"]
        contents = [
            {
                "role": "model" if message.role == "assistant" else "user",
                "parts": [{"text": message.content}],
            }
            for message in request.messages
            if message.role != "system"
        ]
        if not contents:
            raise ValueError("Gemini request requires a user or assistant message")

        generation_config: dict[str, object] = {
            "maxOutputTokens": request.max_output_tokens,
            "temperature": request.temperature,
        }
        if request.json_output:
            generation_config["responseMimeType"] = "application/json"
        if _supports_thinking_budget(self.model) and (
            request.reasoning_budget_tokens is not None
        ):
            # Gemini 2.5 charges thoughts against ``maxOutputTokens``; a structured
            # answer truncated mid-JSON is the symptom of letting it think freely.
            generation_config["thinkingConfig"] = {
                "thinkingBudget": request.reasoning_budget_tokens
            }

        payload: dict[str, object] = {
            "contents": contents,
            "generationConfig": generation_config,
        }
        if system_parts:
            payload["systemInstruction"] = {
                "parts": [{"text": "\n\n".join(system_parts)}]
            }

        model_path = quote(self.model, safe="-._")
        decoded, _ = self._transport.post_json(
            provider=self.name,
            url=f"{self.endpoint}/models/{model_path}:generateContent",
            headers={"x-goog-api-key": self._credentials.api_key},
            payload=payload,
            timeout_seconds=self.timeout_seconds,
        )
        return self._parse_response(decoded)

    def _parse_response(self, decoded: dict[str, object]) -> ProviderResponse:
        feedback = decoded.get("promptFeedback")
        if isinstance(feedback, dict) and feedback.get("blockReason"):
            raise ProviderError(
                provider=self.name,
                code="content_blocked",
                message="Gemini blocked the request",
                retryable=False,
            )

        candidates = decoded.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise ProviderError(
                provider=self.name,
                code="empty_response",
                message="Gemini returned no candidate",
                retryable=False,
            )
        candidate = candidates[0]
        if not isinstance(candidate, dict):
            raise ProviderError(
                provider=self.name,
                code="invalid_response",
                message="Gemini candidate was invalid",
                retryable=False,
            )
        content = candidate.get("content")
        parts = content.get("parts") if isinstance(content, dict) else None
        text = "".join(
            part.get("text", "")
            for part in parts
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        ) if isinstance(parts, list) else ""
        if not text.strip():
            raise ProviderError(
                provider=self.name,
                code="empty_response",
                message="Gemini returned empty content",
                retryable=False,
            )

        usage = decoded.get("usageMetadata")
        usage_dict = usage if isinstance(usage, dict) else {}
        return ProviderResponse(
            provider=self.name,
            model=str(decoded.get("modelVersion") or self.model),
            content=text,
            finish_reason=(
                str(candidate.get("finishReason"))
                if candidate.get("finishReason") is not None
                else None
            ),
            request_id=(
                str(decoded.get("responseId"))
                if decoded.get("responseId") is not None
                else None
            ),
            usage=TokenUsage(
                input_tokens=_optional_int(usage_dict.get("promptTokenCount")),
                cached_input_tokens=_optional_int(
                    usage_dict.get("cachedContentTokenCount")
                ),
                output_tokens=_optional_int(usage_dict.get("candidatesTokenCount")),
                reasoning_tokens=_optional_int(usage_dict.get("thoughtsTokenCount")),
                total_tokens=_optional_int(usage_dict.get("totalTokenCount")),
                source="reported" if usage_dict else "missing",
            ),
        )


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and value >= 0 else None


def _supports_thinking_budget(model: str) -> bool:
    """Only some Gemini models accept ``thinkingConfig``.

    2.5 Flash / Flash-Lite accept a budget (including 0); 2.5 Pro requires a
    minimum, and 1.x models reject the field entirely, so it is only sent where it
    is understood rather than optimistically everywhere.
    """
    name = (model or "").lower()
    return "2.5" in name and "pro" not in name

