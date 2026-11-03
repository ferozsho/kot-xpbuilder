"""Tests for the durable job engine (P2-F05)."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest import mock

from xp_prompt import jobs
from xp_prompt.config import Settings

ROOT_SECRET = "unit-test-root-secret-value"


def settings() -> Settings:
    return Settings.from_env(
        {
            "XP_PROMPT_POSTGRES_HOST": "xp-prompt-db",
            "XP_PROMPT_POSTGRES_PORT": "5432",
            "XP_PROMPT_POSTGRES_USER": "xprompt",
            "XP_PROMPT_POSTGRES_PASSWORD": "database-secret",
            "XP_PROMPT_POSTGRES_DB": "xprompt",
            "XP_PROMPT_REDIS_HOST": "superset-redis",
            "XP_PROMPT_REDIS_PORT": "6379",
            "XP_PROMPT_REDIS_PASSWORD": "redis-secret",
            "XP_PROMPT_REDIS_DB": "2",
            "XP_PROMPT_QDRANT_URL": "http://xp-prompt-qdrant:6333",
            "XP_PROMPT_SERVICE_SECRET": ROOT_SECRET,
        }
    )


def job(
    *,
    status: str = "queued",
    attempt_count: int = 0,
    max_attempts: int = 3,
    steps: tuple[jobs.JobStepRecord, ...] = (),
) -> jobs.JobRecord:
    return jobs.JobRecord(
        id="11111111-1111-1111-1111-111111111111",
        conversation_id="22222222-2222-2222-2222-222222222222",
        job_type=jobs.PING_JOB_TYPE,
        status=status,
        attempt_count=attempt_count,
        max_attempts=max_attempts,
        context_version=1,
        steps=steps,
        created_at=datetime(2026, 11, 3, tzinfo=timezone.utc),
    )


def step(*, key: str = "run", ordinal: int = 1, status: str = "queued"):
    return jobs.JobStepRecord(
        step_key=key, ordinal=ordinal, status=status, attempt_count=0
    )


class RegistryTest(unittest.TestCase):
    def test_ping_is_registered(self) -> None:
        self.assertIn(jobs.PING_JOB_TYPE, jobs.known_job_types())

    def test_duplicate_registration_is_rejected(self) -> None:
        def handler(context: jobs.JobContext) -> dict:
            return {}

        with self.assertRaises(jobs.JobInputError):
            jobs.register_job_handler(jobs.PING_JOB_TYPE)(handler)

    def test_registration_keeps_the_existing_handler_for_the_same_callable(
        self,
    ) -> None:
        handler = jobs.JOB_HANDLERS[jobs.PING_JOB_TYPE]

        self.assertIs(
            jobs.register_job_handler(jobs.PING_JOB_TYPE)(handler), handler
        )


class RequestHashTest(unittest.TestCase):
    def test_key_order_does_not_change_the_hash(self) -> None:
        self.assertEqual(
            jobs.request_hash({"b": 1, "a": 2}),
            jobs.request_hash({"a": 2, "b": 1}),
        )

    def test_different_values_change_the_hash(self) -> None:
        self.assertNotEqual(jobs.request_hash({"a": 1}), jobs.request_hash({"a": 2}))


class ValidationTest(unittest.TestCase):
    def submit(self, **overrides) -> None:
        arguments = {
            "conversation_id": "22222222-2222-2222-2222-222222222222",
            "tenant_key": "default",
            "superset_user_id": 1,
            "job_type": jobs.PING_JOB_TYPE,
            "idempotency_key": "k-1",
            "request": {"echo": "hi"},
        }
        arguments.update(overrides)
        jobs.create_job(settings(), **arguments)

    def test_unknown_job_type_is_rejected(self) -> None:
        with self.assertRaises(jobs.JobInputError):
            self.submit(job_type="teleport")

    def test_missing_idempotency_key_is_rejected(self) -> None:
        with self.assertRaises(jobs.JobInputError):
            self.submit(idempotency_key="  ")

    def test_oversized_request_is_rejected(self) -> None:
        with self.assertRaises(jobs.JobInputError):
            self.submit(request={"blob": "x" * (jobs.MAX_REQUEST_BYTES + 1)})

    def test_too_many_steps_are_rejected(self) -> None:
        with self.assertRaises(jobs.JobInputError):
            self.submit(steps=tuple(f"s{index}" for index in range(jobs.MAX_STEPS + 1)))

    def test_duplicate_steps_are_rejected(self) -> None:
        with self.assertRaises(jobs.JobInputError):
            self.submit(steps=("plan", "plan"))

    def test_attempt_limit_is_bounded(self) -> None:
        with self.assertRaises(jobs.JobInputError):
            self.submit(max_attempts=jobs.MAX_ATTEMPTS_LIMIT + 1)
        with self.assertRaises(jobs.JobInputError):
            self.submit(max_attempts="many")

    def test_blank_step_key_is_rejected(self) -> None:
        with self.assertRaises(jobs.JobInputError):
            self.submit(steps=("ok", "   "))


class TransitionTest(unittest.TestCase):
    def test_running_job_can_be_cancelled(self) -> None:
        self.assertTrue(jobs.can_cancel(job(status=jobs.RUNNING)))

    def test_succeeded_job_cannot_be_cancelled(self) -> None:
        self.assertFalse(jobs.can_cancel(job(status=jobs.SUCCEEDED)))

    def test_failed_job_can_be_retried_while_attempts_remain(self) -> None:
        self.assertTrue(jobs.can_retry(job(status=jobs.FAILED, attempt_count=1)))

    def test_retry_is_refused_once_attempts_are_spent(self) -> None:
        self.assertFalse(
            jobs.can_retry(job(status=jobs.FAILED, attempt_count=3, max_attempts=3))
        )

    def test_succeeded_job_cannot_be_retried(self) -> None:
        self.assertFalse(jobs.can_retry(job(status=jobs.SUCCEEDED)))

    def test_public_dict_exposes_steps_and_state(self) -> None:
        payload = job(
            status=jobs.RUNNING, steps=(step(key="plan"), step(key="run", ordinal=2))
        ).to_public_dict()

        self.assertEqual(payload["status"], "running")
        self.assertEqual([item["step_key"] for item in payload["steps"]], ["plan", "run"])
        self.assertFalse(payload["replayed"])
        self.assertIsNone(payload["error_code"])


class RunJobTest(unittest.TestCase):
    def run_with(self, record: jobs.JobRecord, *, cancel: bool = False):
        with mock.patch.object(jobs, "_load_job_unscoped", return_value=record), \
             mock.patch.object(jobs, "_unseal_request", return_value={"echo": "hi"}), \
             mock.patch.object(jobs, "_cancel_requested", return_value=cancel), \
             mock.patch.object(jobs, "_start_step") as start, \
             mock.patch.object(jobs, "_finish_step") as finish, \
             mock.patch.object(jobs, "_fail_step") as fail, \
             mock.patch.object(jobs, "_cancel_running_job") as cancelled, \
             mock.patch.object(
                 jobs, "_finish_job", side_effect=lambda *args: ("finished", args[2])
             ) as finished:
            result = jobs.run_job(settings(), record.id)
        return result, start, finish, fail, cancelled, finished

    def test_every_step_runs_in_order(self) -> None:
        record = job(
            status=jobs.RUNNING,
            steps=(step(key="plan"), step(key="run", ordinal=2)),
        )

        result, start, finish, fail, cancelled, finished = self.run_with(record)

        self.assertEqual(result[1], jobs.SUCCEEDED)
        self.assertEqual([call.args[2].step_key for call in start.call_args_list], ["plan", "run"])
        self.assertEqual(finish.call_count, 2)
        fail.assert_not_called()
        cancelled.assert_not_called()

    def test_a_terminal_job_is_not_executed(self) -> None:
        record = job(status=jobs.SUCCEEDED, steps=(step(),))

        result, start, _finish, _fail, _cancelled, _finished = self.run_with(record)

        self.assertEqual(result.status, jobs.SUCCEEDED)
        start.assert_not_called()

    def test_cancellation_stops_before_the_next_step(self) -> None:
        record = job(status=jobs.RUNNING, steps=(step(key="plan"), step(key="run", ordinal=2)))

        _, start, _finish, _fail, cancelled, _finished = self.run_with(record, cancel=True)

        cancelled.assert_called_once()
        start.assert_not_called()

    def test_unknown_job_type_fails_closed(self) -> None:
        record = jobs.JobRecord(
            id="11111111-1111-1111-1111-111111111111",
            conversation_id="22222222-2222-2222-2222-222222222222",
            job_type="not-registered",
            status=jobs.RUNNING,
            attempt_count=1,
            max_attempts=3,
            context_version=1,
            steps=(step(),),
        )

        result, _start, _finish, _fail, _cancelled, finished = self.run_with(record)

        self.assertEqual(result[1], jobs.FAILED)
        self.assertEqual(finished.call_args.args[3], "unknown_job_type")

    def test_handler_exception_fails_the_job_with_a_content_free_code(self) -> None:
        record = job(status=jobs.RUNNING, steps=(step(),))

        def boom(_context: jobs.JobContext) -> dict:
            raise TimeoutError("provider secret text")

        with mock.patch.object(jobs, "_load_job_unscoped", return_value=record), \
             mock.patch.object(jobs, "_unseal_request", return_value={}), \
             mock.patch.object(jobs, "_cancel_requested", return_value=False), \
             mock.patch.object(jobs, "_start_step"), \
             mock.patch.object(jobs, "_finish_step"), \
             mock.patch.object(jobs, "_fail_step") as fail, \
             mock.patch.object(
                 jobs, "_finish_job", side_effect=lambda *args: ("finished", args[2], args[3])
             ):
            with mock.patch.dict(jobs.JOB_HANDLERS, {jobs.PING_JOB_TYPE: boom}):
                result = jobs.run_job(settings(), record.id)

        self.assertEqual(result[1], jobs.FAILED)
        self.assertEqual(result[2], "TimeoutError")
        fail.assert_called_once()
        self.assertNotIn("secret", str(result))

    def test_unreadable_request_fails_closed(self) -> None:
        record = job(status=jobs.RUNNING, steps=(step(),))

        with mock.patch.object(jobs, "_load_job_unscoped", return_value=record), \
             mock.patch.object(
                 jobs,
                 "_unseal_request",
                 side_effect=jobs.JobRequestUnreadable("rotated"),
             ), \
             mock.patch.object(
                 jobs, "_finish_job", side_effect=lambda *args: ("finished", args[2], args[3])
             ):
            result = jobs.run_job(settings(), record.id)

        self.assertEqual(result[1], jobs.FAILED)
        self.assertEqual(result[2], "request_unreadable")


class IdentifierTest(unittest.TestCase):
    def test_uuid_shape_is_accepted(self) -> None:
        self.assertTrue(jobs.is_job_id("22222222-2222-2222-2222-222222222222"))

    def test_other_shapes_are_rejected(self) -> None:
        self.assertFalse(jobs.is_job_id("not-a-job"))
        self.assertFalse(jobs.is_job_id(None))


if __name__ == "__main__":
    unittest.main()
