"""Bounded primary/fallback routing for XP Prompt providers.

Policy encoded here (Phase 3, P3-F05):

* a provider may be attempted at most :data:`MAX_ATTEMPTS_PER_PROVIDER` times
  inside one logical request, and only for retryable failures;
* waits use bounded exponential backoff with full jitter and honour
  ``Retry-After`` up to a ceiling, so a rate limit cannot become a retry storm;
* at most one fallback provider is tried, and only when the first provider
  failed retryably — auth, validation and policy failures stop the chain;
* every attempt is reported as its own summary and to an optional health
  callback so passive health state can be persisted.
"""

from __future__ import annotations

import logging
import random
import time
import uuid
from collections.abc import Callable
from dataclasses import replace
from typing import Protocol

from xp_prompt.providers.health import (
    MAX_ATTEMPTS_PER_PROVIDER,
    retry_delay_seconds,
)
from xp_prompt.providers.types import (
    AttemptSummary,
    ProviderError,
    ProviderRequest,
    ProviderResponse,
    ProviderRoutingError,
    RoutedResponse,
)

logger = logging.getLogger(__name__)

#: Called as ``on_attempt(provider_name, error)``; ``error`` is ``None`` on success.
AttemptCallback = Callable[[str, ProviderError | None], None]


class Provider(Protocol):
    name: str
    model: str

    def generate(self, request: ProviderRequest) -> ProviderResponse: ...


class ProviderRouter:
    """Try one primary and at most one compatible fallback provider."""

    def __init__(
        self,
        primary: Provider,
        fallback: Provider | None = None,
        *,
        max_attempts: int = MAX_ATTEMPTS_PER_PROVIDER,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
        on_attempt: AttemptCallback | None = None,
    ) -> None:
        if fallback is not None and fallback.name == primary.name:
            raise ValueError("fallback provider must differ from primary provider")
        self.primary = primary
        self.fallback = fallback
        self.max_attempts = max(1, max_attempts)
        self._sleep = sleep
        self._jitter = jitter
        self._on_attempt = on_attempt

    def generate(self, request: ProviderRequest) -> RoutedResponse:
        correlation_id = request.correlation_id or uuid.uuid4().hex
        request = replace(request, correlation_id=correlation_id)
        attempts: list[AttemptSummary] = []
        try:
            response = self._attempt(self.primary, request, attempts)
        except ProviderError as error:
            if not error.retryable or self.fallback is None:
                raise ProviderRoutingError(
                    cause=error,
                    attempts=tuple(attempts),
                    correlation_id=correlation_id,
                ) from error
            try:
                fallback_response = self._attempt(self.fallback, request, attempts)
            except ProviderError as fallback_error:
                raise ProviderRoutingError(
                    cause=fallback_error,
                    attempts=tuple(attempts),
                    correlation_id=correlation_id,
                ) from fallback_error
            return RoutedResponse(
                response=fallback_response, attempts=tuple(attempts)
            )
        return RoutedResponse(response=response, attempts=tuple(attempts))

    def _attempt(
        self,
        provider: Provider,
        request: ProviderRequest,
        attempts: list[AttemptSummary],
    ) -> ProviderResponse:
        """Run one provider with bounded retries; returns or raises its error."""
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = provider.generate(request)
            except ProviderError as error:
                attempts.append(
                    AttemptSummary(
                        provider=provider.name,
                        model=provider.model,
                        outcome="failed",
                        error_code=error.code,
                        retryable=error.retryable,
                        attempt=attempt,
                        correlation_id=request.correlation_id,
                        operation=request.operation,
                    )
                )
                self._report(provider.name, error)
                self._log(
                    request,
                    provider.name,
                    attempt=attempt,
                    outcome="failed",
                    error_code=error.code,
                )
                if not error.retryable or attempt == self.max_attempts:
                    raise
                delay = retry_delay_seconds(
                    attempt,
                    retry_after_seconds=error.retry_after_seconds,
                    jitter=self._jitter,
                )
                if delay > 0:
                    self._sleep(delay)
                continue
            attempts.append(
                AttemptSummary(
                    provider=provider.name,
                    model=provider.model,
                    outcome="succeeded",
                    attempt=attempt,
                    correlation_id=request.correlation_id,
                    operation=request.operation,
                )
            )
            self._report(provider.name, None)
            self._log(
                request,
                provider.name,
                attempt=attempt,
                outcome="succeeded",
                error_code=None,
            )
            return response
        raise AssertionError("retry loop exited without a result")  # pragma: no cover

    def _log(
        self,
        request: ProviderRequest,
        provider_name: str,
        *,
        attempt: int,
        outcome: str,
        error_code: str | None,
    ) -> None:
        """One structured, content-free line per attempt (P3-F08)."""
        logger.info(
            "provider_attempt correlation_id=%s operation=%s provider=%s "
            "attempt=%d outcome=%s error_code=%s",
            request.correlation_id,
            request.operation,
            provider_name,
            attempt,
            outcome,
            error_code or "-",
        )

    def _report(self, provider_name: str, error: ProviderError | None) -> None:
        if self._on_attempt is None:
            return
        try:
            self._on_attempt(provider_name, error)
        except Exception:  # pragma: no cover - health reporting is best effort
            # Losing a health datapoint must never fail the user's request.
            pass

