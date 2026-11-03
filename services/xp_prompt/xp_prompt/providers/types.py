"""Provider-neutral request, response, usage, and error types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

MessageRole = Literal["system", "user", "assistant"]


@dataclass(frozen=True, slots=True)
class Message:
    role: MessageRole
    content: str

    def __post_init__(self) -> None:
        if not self.content.strip():
            raise ValueError("provider message content must not be empty")


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    operation: str
    messages: tuple[Message, ...]
    max_output_tokens: int
    temperature: float = 0.0
    json_output: bool = False
    #: Keys a ``json_output`` response must contain. An absent key is treated
    #: as a non-retryable validation failure, so a caller never starts acting on
    #: a half-populated document.
    json_required_keys: tuple[str, ...] = ()
    #: Caller-supplied correlation id. The router stamps a generated one when
    #: the caller has no request/job identity yet, so every attempt of one
    #: logical request can be joined in logs and in the future usage ledger.
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        if not self.operation.strip():
            raise ValueError("provider operation must not be empty")
        if not self.messages:
            raise ValueError("provider request must contain at least one message")
        if not 1 <= self.max_output_tokens <= 131_072:
            raise ValueError("max_output_tokens is outside the platform range")
        if not 0.0 <= self.temperature <= 2.0:
            raise ValueError("temperature must be between 0 and 2")
        if self.json_required_keys and not self.json_output:
            raise ValueError(
                "json_required_keys is only meaningful when json_output is set"
            )
        if len(self.json_required_keys) > 32:
            raise ValueError("json_required_keys accepts at most 32 keys")
        for key in self.json_required_keys:
            if not key.strip() or len(key) > 64:
                raise ValueError(
                    "json_required_keys entries must be non-blank and at most "
                    "64 characters"
                )
        if self.correlation_id is not None:
            if not self.correlation_id.strip():
                raise ValueError("correlation_id must not be blank when provided")
            if len(self.correlation_id) > 64:
                raise ValueError("correlation_id must be at most 64 characters")


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input_tokens: int | None
    cached_input_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None
    total_tokens: int | None
    source: Literal["reported", "missing"]


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    provider: str
    model: str
    content: str
    finish_reason: str | None
    request_id: str | None
    usage: TokenUsage


@dataclass(frozen=True, slots=True)
class AttemptSummary:
    provider: str
    model: str
    outcome: Literal["succeeded", "failed"]
    error_code: str | None = None
    retryable: bool = False
    #: 1-based ordinal of this attempt against the same provider.
    attempt: int = 1
    #: Correlation id shared by every attempt of one logical request.
    correlation_id: str | None = None
    #: Operation name, so audit can group attempts without the raw request.
    operation: str | None = None


@dataclass(frozen=True, slots=True)
class RoutedResponse:
    response: ProviderResponse
    attempts: tuple[AttemptSummary, ...]


class ProviderError(RuntimeError):
    """A normalized provider failure safe for routing and audit."""

    def __init__(
        self,
        *,
        provider: str,
        code: str,
        message: str,
        retryable: bool,
        status_code: int | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.code = code
        self.retryable = retryable
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


class ProviderRoutingError(ProviderError):
    """A terminal routing failure retaining every safe attempt summary."""

    def __init__(
        self,
        *,
        cause: ProviderError,
        attempts: tuple[AttemptSummary, ...],
        correlation_id: str | None = None,
    ) -> None:
        super().__init__(
            provider=cause.provider,
            code=cause.code,
            message=str(cause),
            retryable=cause.retryable,
            status_code=cause.status_code,
            retry_after_seconds=cause.retry_after_seconds,
        )
        self.attempts = attempts
        self.correlation_id = correlation_id


@dataclass(frozen=True, slots=True)
class ProviderCredentials:
    """Provider key material retained in memory and excluded from repr."""

    api_key: str = field(repr=False)

