"""Tests for the delivery outbox: exclusivity, retry, dead-letter, no loss.

The properties worth proving are all about failure: two drainers must not take the
same event, a poison event must not block the queue, and an event that keeps
failing must end somewhere an operator can see rather than retrying forever.
"""

from __future__ import annotations

import unittest
from typing import Self
from unittest import mock

from xp_prompt import outbox
from xp_prompt.config import Settings

ROOT_SECRET = "unit-test-root-secret-value"


def settings() -> Settings:
    return Settings.from_env(
        {
            "XP_PROMPT_POSTGRES_HOST": "127.0.0.1",
            "XP_PROMPT_POSTGRES_PORT": "5432",
            "XP_PROMPT_POSTGRES_USER": "xprompt",
            "XP_PROMPT_POSTGRES_PASSWORD": "database-secret",
            "XP_PROMPT_POSTGRES_DB": "xprompt",
            "XP_PROMPT_REDIS_HOST": "127.0.0.1",
            "XP_PROMPT_REDIS_PORT": "6379",
            "XP_PROMPT_REDIS_PASSWORD": "redis-secret",
            "XP_PROMPT_REDIS_DB": "2",
            "XP_PROMPT_QDRANT_URL": "http://xp-prompt-qdrant:6333",
            "XP_PROMPT_SERVICE_SECRET": ROOT_SECRET,
        }
    )


class _FakeCursor:
    def __init__(self, rows: list[dict | None] | None = None) -> None:
        self._rows = list(rows or [])
        self.statements: list[str] = []
        self.params: list[object] = []
        self._current: dict | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: object = None) -> None:
        self.statements.append(sql)
        self.params.append(params)
        self._current = self._rows.pop(0) if self._rows else None

    def fetchone(self) -> dict | None:
        return self._current

    def fetchall(self) -> list:
        return [self._current] if self._current else []


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def cursor(self) -> _FakeCursor:
        return self._cursor


def event(event_type: str = "usage.attempt", attempt_count: int = 0) -> dict:
    return {
        "id": "33333333-3333-3333-3333-333333333333",
        "aggregate_type": "ai_provider_attempt",
        "aggregate_id": "44444444-4444-4444-4444-444444444444",
        "event_type": event_type,
        "payload": {"tenant_id": "t"},
        "status": "delivering",
        "attempt_count": attempt_count,
        "last_error": None,
        "available_at": None,
        "delivered_at": None,
    }


class EmitTests(unittest.TestCase):
    def test_an_event_needs_a_tenant(self) -> None:
        with self.assertRaises(outbox.OutboxError) as caught:
            outbox.emit(
                settings(),
                tenant_id=None,
                aggregate_type="ai_provider_attempt",
                event_type="usage.attempt",
                payload={},
            )
        self.assertEqual(caught.exception.code, "tenant_required")

    def test_an_event_needs_a_type(self) -> None:
        with self.assertRaises(outbox.OutboxError) as caught:
            outbox.emit(
                settings(),
                tenant_id="11111111-1111-1111-1111-111111111111",
                aggregate_type="ai_provider_attempt",
                event_type="  ",
                payload={},
            )
        self.assertEqual(caught.exception.code, "invalid_event")

    def test_an_event_needs_an_object_payload(self) -> None:
        with self.assertRaises(outbox.OutboxError):
            outbox.emit(
                settings(),
                tenant_id="11111111-1111-1111-1111-111111111111",
                aggregate_type="ai_provider_attempt",
                event_type="usage.attempt",
                payload=["not", "an", "object"],
            )


class ClaimTests(unittest.TestCase):
    def test_claiming_is_exclusive_and_skips_locked_rows(self) -> None:
        cursor = _FakeCursor([event()])
        rows = outbox._claim(cursor, 10, "usage.attempt", None)

        statement = cursor.statements[0]
        self.assertIn("FOR UPDATE SKIP LOCKED", statement)
        self.assertIn("status = 'pending'", statement)
        self.assertIn("available_at <= CURRENT_TIMESTAMP", statement)
        self.assertEqual(len(rows), 1)

    def test_claiming_marks_the_row_as_delivering(self) -> None:
        cursor = _FakeCursor([event()])
        outbox._claim(cursor, 10, None, None)
        self.assertIn("SET status = 'delivering'", cursor.statements[0])

    def test_a_batch_is_bounded(self) -> None:
        cursor = _FakeCursor([])
        outbox._claim(cursor, 10_000, None, None)
        self.assertEqual(cursor.params[0][-1], outbox.MAX_BATCH)


class RetryTests(unittest.TestCase):
    """A failed delivery is counted, backed off, and eventually given up on."""

    def fail_delivery(self, rows):
        cursor = _FakeCursor(rows)
        with mock.patch.object(
            outbox, "connect", return_value=_FakeConnection(cursor)
        ):
            status = outbox.record_failure(settings(), "event-1", "boom")
        return status, cursor

    def test_a_first_failure_schedules_a_retry(self) -> None:
        status, cursor = self.fail_delivery([{"attempt_count": 0}, {"status": "pending"}])

        self.assertEqual(status, "pending")
        update = cursor.statements[1]
        self.assertIn("attempt_count", update)
        self.assertIn("available_at", update)
        self.assertIn("last_error", update)

    def test_the_attempt_count_increments(self) -> None:
        _, cursor = self.fail_delivery([{"attempt_count": 2}, {"status": "pending"}])
        self.assertEqual(cursor.params[1][1], 3)

    def test_the_final_failure_dead_letters_the_event(self) -> None:
        status, _ = self.fail_delivery(
            [
                {"attempt_count": outbox.MAX_ATTEMPTS - 1},
                {"status": "dead_letter"},
            ]
        )
        self.assertEqual(status, "dead_letter")

    def test_backoff_grows_and_is_capped(self) -> None:
        first = outbox._backoff(1).total_seconds()
        third = outbox._backoff(3).total_seconds()
        late = outbox._backoff(50).total_seconds()

        self.assertEqual(first, outbox.BASE_BACKOFF_SECONDS)
        self.assertGreater(third, first)
        self.assertEqual(late, outbox.MAX_BACKOFF_SECONDS)

    def test_requeueing_requires_a_dead_lettered_event(self) -> None:
        cursor = _FakeCursor([None])
        with mock.patch.object(
            outbox, "connect", return_value=_FakeConnection(cursor)
        ), self.assertRaises(outbox.OutboxError) as caught:
            outbox.requeue(settings(), "event-1")
        self.assertEqual(caught.exception.code, "not_dead_letter")


class DeliverTests(unittest.TestCase):
    """One poison event must not take the batch down with it."""

    def run_batch(self, events, handlers):
        claimed = [outbox.Event(**{**item, "id": item["id"]}) for item in events]
        failures: list[tuple] = []
        with (
            mock.patch.object(outbox, "claim", return_value=claimed),
            mock.patch.object(
                outbox, "connect", return_value=_FakeConnection(_FakeCursor([]))
            ),
            mock.patch.object(
                outbox,
                "record_failure",
                side_effect=lambda *args, **kwargs: failures.append(args) or "pending",
            ),
        ):
            outcome = outbox.deliver(settings(), handlers=handlers)
        return outcome, failures

    def test_a_delivered_event_is_counted(self) -> None:
        outcome, failures = self.run_batch(
            [event()], {"usage.attempt": lambda cursor, payload: True}
        )
        self.assertEqual(outcome["delivered"], 1)
        self.assertEqual(failures, [])

    def test_a_duplicate_is_counted_separately_from_a_delivery(self) -> None:
        outcome, _ = self.run_batch(
            [event()], {"usage.attempt": lambda cursor, payload: False}
        )
        self.assertEqual(outcome["duplicates"], 1)
        self.assertEqual(outcome["delivered"], 0)

    def test_a_raising_handler_is_recorded_as_a_failure(self) -> None:
        def boom(cursor, payload):
            raise RuntimeError("database went away")

        outcome, failures = self.run_batch([event()], {"usage.attempt": boom})

        self.assertEqual(outcome["failed"], 1)
        self.assertEqual(len(failures), 1)
        self.assertIn("database went away", failures[0][2])

    def test_an_event_with_no_handler_fails_instead_of_vanishing(self) -> None:
        outcome, failures = self.run_batch([event("mystery.event")], {})
        self.assertEqual(outcome["failed"], 1)
        self.assertIn("no handler", failures[0][2])

    def test_one_failure_does_not_stop_the_rest_of_the_batch(self) -> None:
        def picky(cursor, payload):
            if payload.get("bad"):
                raise RuntimeError("nope")
            return True

        good = event()
        bad = event()
        bad["id"] = "55555555-5555-5555-5555-555555555555"
        bad["payload"] = {"bad": True}

        outcome, _failures = self.run_batch(
            [bad, good], {"usage.attempt": picky}
        )

        self.assertEqual(outcome["claimed"], 2)
        self.assertEqual(outcome["failed"], 1)
        self.assertEqual(outcome["delivered"], 1)


if __name__ == "__main__":
    unittest.main()
