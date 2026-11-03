"""Tests for passive provider health, the circuit breaker, and retry pacing."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from xp_prompt.providers.health import (
    BASE_RETRY_DELAY_SECONDS,
    FAILURE_THRESHOLD,
    MAX_OPEN_SECONDS,
    MAX_RETRY_DELAY_SECONDS,
    OPEN_SECONDS,
    ProviderHealth,
    after_failure,
    after_success,
    retry_delay_seconds,
)

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)


class ProviderHealthStateTest(unittest.TestCase):
    def test_a_fresh_provider_is_closed(self) -> None:
        health = ProviderHealth()

        self.assertFalse(health.is_open(NOW))
        self.assertEqual(health.to_public_dict(NOW)["state"], "closed")

    def test_breaker_opens_on_the_threshold_failure(self) -> None:
        health = ProviderHealth()
        for _ in range(FAILURE_THRESHOLD - 1):
            health = after_failure(
                health, error_code="http_503", retryable=True, now=NOW
            )
            self.assertFalse(health.is_open(NOW))

        health = after_failure(
            health, error_code="http_503", retryable=True, now=NOW
        )

        self.assertTrue(health.is_open(NOW))
        self.assertEqual(health.last_error_code, "http_503")
        self.assertEqual(
            health.circuit_open_until,
            NOW + timedelta(seconds=OPEN_SECONDS),
        )

    def test_breaker_closes_by_the_clock(self) -> None:
        health = after_failure(
            ProviderHealth(consecutive_failures=FAILURE_THRESHOLD - 1),
            error_code="transport_error",
            retryable=True,
            now=NOW,
        )

        self.assertTrue(health.is_open(NOW))
        later = NOW + timedelta(seconds=OPEN_SECONDS + 1)
        self.assertFalse(health.is_open(later))
        self.assertEqual(health.to_public_dict(later)["state"], "closed")

    def test_repeated_failures_double_the_cooldown_but_stay_capped(self) -> None:
        health = ProviderHealth(consecutive_failures=FAILURE_THRESHOLD)
        window = health.open_seconds()

        health = after_failure(health, error_code="http_503", retryable=True, now=NOW)

        self.assertEqual(window, OPEN_SECONDS)
        self.assertEqual(health.open_seconds(), OPEN_SECONDS * 2)
        self.assertEqual(
            after_failure(
                ProviderHealth(consecutive_failures=50),
                error_code="http_503",
                retryable=True,
                now=NOW,
            ).open_seconds(),
            MAX_OPEN_SECONDS,
        )

    def test_success_clears_the_failure_streak(self) -> None:
        health = after_success()

        self.assertEqual(health.consecutive_failures, 0)
        self.assertIsNone(health.circuit_open_until)
        self.assertIsNone(health.last_error_code)

    def test_non_retryable_failures_still_count_toward_the_breaker(self) -> None:
        health = ProviderHealth(consecutive_failures=FAILURE_THRESHOLD - 1)

        health = after_failure(
            health, error_code="http_401", retryable=False, now=NOW
        )

        self.assertTrue(health.is_open(NOW))
        self.assertEqual(health.last_error_code, "http_401")

    def test_public_payload_never_contains_prompt_or_key_material(self) -> None:
        payload = after_failure(
            ProviderHealth(), error_code="http_503", retryable=True, now=NOW
        ).to_public_dict(NOW)

        self.assertEqual(
            set(payload),
            {
                "state",
                "consecutive_failures",
                "open_until",
                "last_error",
                "last_failure_at",
            },
        )


class RetryPacingTest(unittest.TestCase):
    def test_backoff_doubles_and_is_capped(self) -> None:
        delays = [
            retry_delay_seconds(attempt, jitter=lambda: 1.0)
            for attempt in range(1, 6)
        ]

        self.assertEqual(delays[0], BASE_RETRY_DELAY_SECONDS)
        self.assertEqual(delays[1], BASE_RETRY_DELAY_SECONDS * 2)
        self.assertEqual(delays[2], BASE_RETRY_DELAY_SECONDS * 4)
        self.assertEqual(delays[3], BASE_RETRY_DELAY_SECONDS * 8)
        self.assertEqual(delays[4], MAX_RETRY_DELAY_SECONDS)

    def test_jitter_stays_inside_the_ceiling(self) -> None:
        self.assertEqual(retry_delay_seconds(3, jitter=lambda: 0.0), 0.0)
        self.assertLessEqual(
            retry_delay_seconds(3, jitter=lambda: 0.999),
            BASE_RETRY_DELAY_SECONDS * 4,
        )

    def test_retry_after_is_honoured_but_bounded(self) -> None:
        self.assertEqual(
            retry_delay_seconds(1, retry_after_seconds=2, jitter=lambda: 1.0), 2.0
        )
        self.assertEqual(
            retry_delay_seconds(1, retry_after_seconds=900, jitter=lambda: 1.0),
            MAX_RETRY_DELAY_SECONDS,
        )


if __name__ == "__main__":
    unittest.main()
