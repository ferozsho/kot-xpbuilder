"""Bounded primary/fallback routing for XP Prompt providers."""

from __future__ import annotations

from typing import Protocol

from xp_prompt.providers.types import (
    AttemptSummary,
    ProviderError,
    ProviderRequest,
    ProviderResponse,
    ProviderRoutingError,
    RoutedResponse,
)


class Provider(Protocol):
    name: str
    model: str

    def generate(self, request: ProviderRequest) -> ProviderResponse: ...


class ProviderRouter:
    """Try one primary and at most one compatible fallback provider."""

    def __init__(self, primary: Provider, fallback: Provider | None = None) -> None:
        if fallback is not None and fallback.name == primary.name:
            raise ValueError("fallback provider must differ from primary provider")
        self.primary = primary
        self.fallback = fallback

    def generate(self, request: ProviderRequest) -> RoutedResponse:
        attempts: list[AttemptSummary] = []
        try:
            response = self.primary.generate(request)
        except ProviderError as error:
            attempts.append(
                AttemptSummary(
                    provider=self.primary.name,
                    model=self.primary.model,
                    outcome="failed",
                    error_code=error.code,
                    retryable=error.retryable,
                )
            )
            if not error.retryable or self.fallback is None:
                raise ProviderRoutingError(
                    cause=error, attempts=tuple(attempts)
                ) from error
        else:
            attempts.append(
                AttemptSummary(
                    provider=self.primary.name,
                    model=self.primary.model,
                    outcome="succeeded",
                )
            )
            return RoutedResponse(response=response, attempts=tuple(attempts))

        try:
            fallback_response = self.fallback.generate(request)
        except ProviderError as error:
            attempts.append(
                AttemptSummary(
                    provider=self.fallback.name,
                    model=self.fallback.model,
                    outcome="failed",
                    error_code=error.code,
                    retryable=error.retryable,
                )
            )
            raise ProviderRoutingError(
                cause=error, attempts=tuple(attempts)
            ) from error

        attempts.append(
            AttemptSummary(
                provider=self.fallback.name,
                model=self.fallback.model,
                outcome="succeeded",
            )
        )
        return RoutedResponse(response=fallback_response, attempts=tuple(attempts))

