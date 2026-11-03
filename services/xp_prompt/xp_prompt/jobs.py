"""Durable job engine for the XP Prompt workspace (P2-F05).

A job is the only way a turn is allowed to do multi-step work: the workspace
submits one, a worker claims it, and every state change is written to
``ai_job`` / ``ai_job_step`` so a browser refresh (or a dead worker) resumes from
the database rather than from memory.

Design rules, in the order they matter:

* **the database is the queue state** — Celery only carries the job id. A worker
  that dies leaves a lease that expires, and :func:`recover_expired` moves the job
  back to ``queued`` (or to ``failed`` once ``max_attempts`` is spent), so a
  refresh can never produce duplicate artifacts from a phantom running job;
* **claiming is a compare-and-set** — a job is claimed with
  ``... WHERE status = 'queued' AND attempt_count < max_attempts``, so a
  redelivered message cannot run the same job twice at the same time;
* **the request is sealed** — the payload is stored with :class:`SecretBox`
  (migration ``20261103_05``), because recovery needs it but a database dump must
  not describe a user's question in plain text;
* **cancellation is a flag, not a kill** — the runner re-reads
  ``cancel_requested_at`` between steps, so a cancel always takes effect at a step
  boundary and the job ends ``cancelled`` instead of half-applied;
* **failure is normalized** — only an error *code* and the exception class name
  are stored, never a message that could quote the request or a provider body.

Job types register a handler; until Phases 4–8 register theirs, only ``ping``
exists, which is what the workspace and the tests use to prove the shell works.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from xp_prompt.config import Settings
from xp_prompt.db import connect
from xp_prompt.security import SecretBox

logger = logging.getLogger(__name__)

QUEUED = "queued"
RUNNING = "running"
CLARIFICATION = "clarification"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"

JOB_STATUSES: tuple[str, ...] = (
    QUEUED,
    RUNNING,
    CLARIFICATION,
    SUCCEEDED,
    FAILED,
    CANCELLED,
)
TERMINAL_STATUSES = frozenset({SUCCEEDED, FAILED, CANCELLED})

STEP_QUEUED = "queued"
STEP_RUNNING = "running"
STEP_SUCCEEDED = "succeeded"
STEP_FAILED = "failed"
STEP_CANCELLED = "cancelled"
STEP_SKIPPED = "skipped"

DEFAULT_MAX_ATTEMPTS = 3
MAX_ATTEMPTS_LIMIT = 10
#: A lease longer than the worker's hard time limit (330s) would keep a wedged
#: job invisible to recovery for longer than necessary.
DEFAULT_LEASE_SECONDS = 300
MAX_LEASE_SECONDS = 900

MAX_JOB_TYPE_CHARS = 48
MAX_STEP_KEY_CHARS = 64
MAX_STEPS = 20
MAX_IDEMPOTENCY_KEY_CHARS = 128
MAX_REQUEST_BYTES = 64 * 1024
MAX_ERROR_CODE_CHARS = 64

#: Built-in job type used by the workspace and the tests to exercise the shell.
PING_JOB_TYPE = "ping"


class JobNotFound(LookupError):
    """No job with that id belongs to this tenant/user pair."""


class JobInputError(ValueError):
    """A rejected job submission (safe to return to the UI)."""


class JobIdempotencyConflict(RuntimeError):
    """The same idempotency key was reused for a different request."""


class JobStateError(RuntimeError):
    """The job is not in a state that allows the requested transition."""


class JobRequestUnreadable(RuntimeError):
    """The sealed request cannot be decrypted (root secret rotated)."""


@dataclass(frozen=True, slots=True)
class JobStepRecord:
    step_key: str
    ordinal: int
    status: str
    attempt_count: int
    error_code: str | None = None

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "step_key": self.step_key,
            "ordinal": self.ordinal,
            "status": self.status,
            "attempt_count": self.attempt_count,
            "error_code": self.error_code,
        }


@dataclass(frozen=True, slots=True)
class JobRecord:
    id: str
    conversation_id: str
    job_type: str
    status: str
    attempt_count: int
    max_attempts: int
    context_version: int
    error_code: str | None = None
    cancel_requested: bool = False
    created_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    #: True when ``create_job`` returned an existing job for the same
    #: idempotency key instead of inserting a new one.
    replayed: bool = False
    steps: tuple[JobStepRecord, ...] = field(default_factory=tuple)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "conversation_id": self.conversation_id,
            "job_type": self.job_type,
            "status": self.status,
            "attempt_count": self.attempt_count,
            "max_attempts": self.max_attempts,
            "context_version": self.context_version,
            "cancel_requested": self.cancel_requested,
            "error_code": self.error_code,
            "replayed": self.replayed,
            "created_at": _isoformat(self.created_at),
            "started_at": _isoformat(self.started_at),
            "completed_at": _isoformat(self.completed_at),
            "steps": [step.to_public_dict() for step in self.steps],
        }


@dataclass(frozen=True, slots=True)
class JobContext:
    """What a handler receives: the job, the step, and the decrypted request."""

    settings: Settings
    job_id: str
    step_key: str
    request: dict[str, Any]
    context_version: int


JobHandler = Callable[[JobContext], dict[str, Any]]

JOB_HANDLERS: dict[str, JobHandler] = {}


def register_job_handler(job_type: str) -> Callable[[JobHandler], JobHandler]:
    """Register the handler for one job type (Phases 4–8 add theirs)."""

    def decorator(handler: JobHandler) -> JobHandler:
        key = (job_type or "").strip()
        if not key or len(key) > MAX_JOB_TYPE_CHARS:
            raise JobInputError("job_type must be 1-48 characters")
        if key in JOB_HANDLERS and JOB_HANDLERS[key] is not handler:
            raise JobInputError(f"job_type {key} is already registered")
        JOB_HANDLERS[key] = handler
        return handler

    return decorator


def known_job_types() -> tuple[str, ...]:
    return tuple(sorted(JOB_HANDLERS))


@register_job_handler(PING_JOB_TYPE)
def _ping_handler(context: JobContext) -> dict[str, Any]:
    """No-op step used to prove submission, progress, cancel, and recovery."""
    return {"step": context.step_key, "context_version": context.context_version}


def request_hash(request: dict[str, Any]) -> str:
    """Stable hash of a job request, used to detect idempotency-key reuse."""
    canonical = json.dumps(request, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def can_cancel(job: JobRecord) -> bool:
    return not job.is_terminal or job.status == CLARIFICATION


def can_retry(job: JobRecord) -> bool:
    """A failed or cancelled job can be retried while attempts remain."""
    return job.status in {FAILED, CANCELLED} and job.attempt_count < job.max_attempts


def create_job(
    settings: Settings,
    *,
    conversation_id: str,
    tenant_key: str,
    superset_user_id: int,
    job_type: str,
    idempotency_key: str,
    request: dict[str, Any],
    steps: tuple[str, ...] = (),
    context_version: int = 1,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> JobRecord:
    """Insert a job (and its steps), replays the existing one for the same key."""
    job_type = (job_type or "").strip()
    if job_type not in JOB_HANDLERS:
        raise JobInputError(f"Unsupported job type: {job_type or '(empty)'}")
    key = (idempotency_key or "").strip()
    if not key or len(key) > MAX_IDEMPOTENCY_KEY_CHARS:
        raise JobInputError(
            f"idempotency_key must be 1-{MAX_IDEMPOTENCY_KEY_CHARS} characters"
        )
    if not isinstance(request, dict):
        raise JobInputError("request must be an object")
    payload = _sealable(request)
    step_keys = _clean_steps(steps)
    try:
        attempts = int(max_attempts)
    except (TypeError, ValueError) as exc:
        raise JobInputError("max_attempts must be an integer") from exc
    if not 1 <= attempts <= MAX_ATTEMPTS_LIMIT:
        raise JobInputError(
            f"max_attempts must be between 1 and {MAX_ATTEMPTS_LIMIT}"
        )
    digest = request_hash(request)
    box = SecretBox.from_root_secret(settings.service_secret)
    sealed = box.encrypt(payload).encode("utf-8")

    with connect(settings) as connection:
        with connection.cursor() as cursor:
            tenant_id = _tenant_id(cursor, tenant_key)
            _assert_conversation(cursor, conversation_id, tenant_id, superset_user_id)
            cursor.execute(
                """
                INSERT INTO ai_job (
                    tenant_id, conversation_id, superset_user_id, job_type,
                    idempotency_key, request_hash, request_ciphertext,
                    context_version, policy_version, authorization_version,
                    max_attempts
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 1, 1, %s)
                ON CONFLICT (tenant_id, superset_user_id, idempotency_key)
                DO NOTHING
                RETURNING id
                """,
                (
                    tenant_id,
                    conversation_id,
                    int(superset_user_id),
                    job_type,
                    key,
                    digest,
                    sealed,
                    max(1, int(context_version)),
                    attempts,
                ),
            )
            row = cursor.fetchone()
            replayed = row is None
            if replayed:
                cursor.execute(
                    """
                    SELECT *
                    FROM ai_job
                    WHERE tenant_id = %s
                      AND superset_user_id = %s
                      AND idempotency_key = %s
                    """,
                    (tenant_id, int(superset_user_id), key),
                )
                row = cursor.fetchone()
                if row is None:  # pragma: no cover - only under a deleted row
                    raise JobInputError("job could not be created")
                if str(row["request_hash"]) != digest:
                    raise JobIdempotencyConflict(
                        "idempotency_key was already used for a different request"
                    )
            else:
                job_id = str(row["id"])
                for ordinal, step_key in enumerate(step_keys, start=1):
                    cursor.execute(
                        """
                        INSERT INTO ai_job_step (tenant_id, job_id, step_key, ordinal)
                        VALUES (%s, %s, %s, %s)
                        """,
                        (tenant_id, job_id, step_key, ordinal),
                    )

    record = get_job(
        settings,
        str(row["id"]),
        tenant_key=tenant_key,
        superset_user_id=int(superset_user_id),
    )
    if record is None:  # pragma: no cover - defensive
        raise JobInputError("job was created but cannot be read back")
    if replayed:
        record = _replace(record, replayed=True)
    return record


def get_job(
    settings: Settings,
    job_id: str,
    *,
    tenant_key: str,
    superset_user_id: int,
) -> JobRecord | None:
    """Load one job (with its steps), scoped to its owner."""
    with connect(settings) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT j.*
                FROM ai_job j
                JOIN ai_tenant t ON t.id = j.tenant_id
                WHERE j.id = %s
                  AND t.key = %s
                  AND j.superset_user_id = %s
                """,
                (job_id, tenant_key, int(superset_user_id)),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            steps = _load_steps(cursor, job_id)
    return _job_from_row(row, steps)


def list_jobs(
    settings: Settings,
    conversation_id: str,
    *,
    tenant_key: str,
    superset_user_id: int,
    limit: int = 20,
) -> list[JobRecord]:
    """Jobs of one conversation, newest first, so a reload can resume them."""
    bounded = max(1, min(int(limit), 100))
    with connect(settings) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT j.*
                FROM ai_job j
                JOIN ai_tenant t ON t.id = j.tenant_id
                WHERE j.conversation_id = %s
                  AND t.key = %s
                  AND j.superset_user_id = %s
                ORDER BY j.created_at DESC
                LIMIT %s
                """,
                (conversation_id, tenant_key, int(superset_user_id), bounded),
            )
            rows = cursor.fetchall()
            return [_job_from_row(row, _load_steps(cursor, str(row["id"]))) for row in rows]


def request_cancel(
    settings: Settings,
    job_id: str,
    *,
    tenant_key: str,
    superset_user_id: int,
) -> JobRecord:
    """Flag a job for cancellation; a queued job is cancelled immediately."""
    with connect(settings) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE ai_job j
                SET cancel_requested_at = COALESCE(j.cancel_requested_at, CURRENT_TIMESTAMP),
                    status = CASE WHEN j.status = 'queued' THEN 'cancelled' ELSE j.status END,
                    completed_at = CASE
                        WHEN j.status = 'queued' THEN CURRENT_TIMESTAMP
                        ELSE j.completed_at
                    END,
                    updated_at = CURRENT_TIMESTAMP
                FROM ai_tenant t
                WHERE j.tenant_id = t.id
                  AND t.key = %s
                  AND j.id = %s
                  AND j.superset_user_id = %s
                  AND j.status IN ('queued', 'running', 'clarification')
                RETURNING j.id
                """,
                (tenant_key, job_id, int(superset_user_id)),
            )
            updated = cursor.fetchone()
            if updated is None:
                raise JobNotFound("job not found")

    record = get_job(
        settings,
        job_id,
        tenant_key=tenant_key,
        superset_user_id=int(superset_user_id),
    )
    if record is None:  # pragma: no cover - defensive
        raise JobNotFound("job not found")
    return record


def retry_job(
    settings: Settings,
    job_id: str,
    *,
    tenant_key: str,
    superset_user_id: int,
) -> JobRecord:
    """Requeue a failed or cancelled job whose attempts are not exhausted."""
    record = get_job(
        settings,
        job_id,
        tenant_key=tenant_key,
        superset_user_id=int(superset_user_id),
    )
    if record is None:
        raise JobNotFound("job not found")
    if not can_retry(record):
        raise JobStateError("job cannot be retried")

    with connect(settings) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE ai_job j
                SET status = 'queued',
                    error_code = NULL,
                    error_summary = NULL,
                    completed_at = NULL,
                    started_at = NULL,
                    cancel_requested_at = NULL,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    updated_at = CURRENT_TIMESTAMP
                FROM ai_tenant t
                WHERE j.tenant_id = t.id
                  AND t.key = %s
                  AND j.id = %s
                  AND j.superset_user_id = %s
                  AND j.status IN ('failed', 'cancelled')
                  AND j.attempt_count < j.max_attempts
                RETURNING j.id
                """,
                (tenant_key, job_id, int(superset_user_id)),
            )
            if cursor.fetchone() is None:  # pragma: no cover - lost race
                raise JobStateError("job cannot be retried")
            cursor.execute(
                """
                UPDATE ai_job_step
                SET status = 'queued',
                    started_at = NULL,
                    completed_at = NULL,
                    error_code = NULL,
                    error_summary = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE job_id = %s
                """,
                (job_id,),
            )

    refreshed = get_job(
        settings,
        job_id,
        tenant_key=tenant_key,
        superset_user_id=int(superset_user_id),
    )
    if refreshed is None:  # pragma: no cover - defensive
        raise JobNotFound("job not found")
    return refreshed


def claim_job(
    settings: Settings,
    job_id: str,
    *,
    worker_id: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> JobRecord | None:
    """Atomically move a queued job to running; ``None`` when not claimable.

    The compare-and-set on ``status``/``attempt_count`` is what makes a
    redelivered broker message harmless: the second claim matches no row.
    """
    lease = max(1, min(int(lease_seconds), MAX_LEASE_SECONDS))
    with connect(settings) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE ai_job
                SET status = 'running',
                    lease_owner = %s,
                    lease_expires_at = CURRENT_TIMESTAMP + (%s * INTERVAL '1 second'),
                    attempt_count = attempt_count + 1,
                    started_at = COALESCE(started_at, CURRENT_TIMESTAMP),
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                  AND status = 'queued'
                  AND cancel_requested_at IS NULL
                  AND attempt_count < max_attempts
                RETURNING *
                """,
                (worker_id, lease, job_id),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            steps = _load_steps(cursor, job_id)
    return _job_from_row(row, steps)


def recover_expired(settings: Settings) -> list[tuple[str, str]]:
    """Requeue (or fail) jobs whose worker lease expired.

    Returns ``(job_id, new_status)`` pairs for the caller to log or re-enqueue.
    """
    with connect(settings) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE ai_job
                SET status = CASE
                        WHEN attempt_count < max_attempts THEN 'queued'
                        ELSE 'failed'
                    END,
                    error_code = CASE
                        WHEN attempt_count < max_attempts THEN error_code
                        ELSE 'lease_expired'
                    END,
                    completed_at = CASE
                        WHEN attempt_count < max_attempts THEN NULL
                        ELSE CURRENT_TIMESTAMP
                    END,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE status = 'running'
                  AND lease_expires_at IS NOT NULL
                  AND lease_expires_at < CURRENT_TIMESTAMP
                RETURNING id, status
                """,
            )
            rows = cursor.fetchall()
    return [(str(row["id"]), str(row["status"])) for row in rows]


def pending_job_ids(settings: Settings, *, limit: int = 50) -> list[str]:
    """Queued jobs, so a sweep can re-enqueue work a lost broker message dropped."""
    bounded = max(1, min(int(limit), 200))
    with connect(settings) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id
                FROM ai_job
                WHERE status = 'queued'
                  AND cancel_requested_at IS NULL
                  AND attempt_count < max_attempts
                ORDER BY created_at
                LIMIT %s
                """,
                (bounded,),
            )
            return [str(row["id"]) for row in cursor.fetchall()]


def run_job(settings: Settings, job_id: str) -> JobRecord:
    """Execute a claimed job step by step, honouring cancellation between steps.

    ``run_job`` does not claim: the caller claims first, so a redelivered task
    returns early instead of running the work twice.
    """
    job = _load_job_unscoped(settings, job_id)
    if job is None:
        raise JobNotFound("job not found")
    if job.status != RUNNING:
        return job

    handler = JOB_HANDLERS.get(job.job_type)
    if handler is None:
        return _finish_job(settings, job_id, FAILED, "unknown_job_type")

    try:
        request = _unseal_request(settings, job_id)
    except JobRequestUnreadable:
        return _finish_job(settings, job_id, FAILED, "request_unreadable")

    for step in job.steps:
        if step.status in {STEP_SUCCEEDED, STEP_SKIPPED}:
            continue
        if _cancel_requested(settings, job_id):
            return _cancel_running_job(settings, job_id)
        _start_step(settings, job_id, step)
        try:
            output = handler(
                JobContext(
                    settings=settings,
                    job_id=job_id,
                    step_key=step.step_key,
                    request=request,
                    context_version=job.context_version,
                )
            )
        except Exception as exc:  # handler failures must not kill the worker
            code = _error_code(exc)
            logger.info(
                "job_step job=%s step=%s outcome=failed error_code=%s",
                job_id,
                step.step_key,
                code,
            )
            _fail_step(settings, job_id, step, code)
            return _finish_job(settings, job_id, FAILED, code)
        _finish_step(settings, job_id, step, output)
        logger.info("job_step job=%s step=%s outcome=succeeded", job_id, step.step_key)

    if _cancel_requested(settings, job_id):
        return _cancel_running_job(settings, job_id)
    logger.info("job_finished job=%s outcome=succeeded", job_id)
    return _finish_job(settings, job_id, SUCCEEDED, None)


# --- internals --------------------------------------------------------


def _load_job_unscoped(settings: Settings, job_id: str) -> JobRecord | None:
    with connect(settings) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM ai_job WHERE id = %s", (job_id,))
            row = cursor.fetchone()
            if row is None:
                return None
            steps = _load_steps(cursor, job_id)
    return _job_from_row(row, steps)


def _unseal_request(settings: Settings, job_id: str) -> dict[str, Any]:
    with connect(settings) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT request_ciphertext FROM ai_job WHERE id = %s", (job_id,)
            )
            row = cursor.fetchone()
    if row is None or not row.get("request_ciphertext"):
        return {}
    try:
        box = SecretBox.from_root_secret(settings.service_secret)
        raw = box.decrypt(bytes(row["request_ciphertext"]).decode("ascii"))
        value = json.loads(raw)
    except (RuntimeError, ValueError, UnicodeDecodeError) as exc:
        raise JobRequestUnreadable("job request cannot be decrypted") from exc
    return value if isinstance(value, dict) else {}


def _cancel_requested(settings: Settings, job_id: str) -> bool:
    with connect(settings) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT cancel_requested_at FROM ai_job WHERE id = %s", (job_id,)
            )
            row = cursor.fetchone()
    return bool(row and row.get("cancel_requested_at"))


def _start_step(settings: Settings, job_id: str, step: JobStepRecord) -> None:
    with connect(settings) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE ai_job_step
                SET status = 'running',
                    attempt_count = attempt_count + 1,
                    started_at = COALESCE(started_at, CURRENT_TIMESTAMP),
                    updated_at = CURRENT_TIMESTAMP
                WHERE job_id = %s AND step_key = %s AND ordinal = %s
                """,
                (job_id, step.step_key, step.ordinal),
            )


def _finish_step(
    settings: Settings,
    job_id: str,
    step: JobStepRecord,
    output: dict[str, Any],
) -> None:
    with connect(settings) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE ai_job_step
                SET status = 'succeeded',
                    completed_at = CURRENT_TIMESTAMP,
                    output_ref = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE job_id = %s AND step_key = %s AND ordinal = %s
                """,
                (_reference(output), job_id, step.step_key, step.ordinal),
            )


def _fail_step(
    settings: Settings, job_id: str, step: JobStepRecord, code: str
) -> None:
    with connect(settings) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE ai_job_step
                SET status = 'failed',
                    completed_at = CURRENT_TIMESTAMP,
                    error_code = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE job_id = %s AND step_key = %s AND ordinal = %s
                """,
                (code, job_id, step.step_key, step.ordinal),
            )


def _cancel_running_job(settings: Settings, job_id: str) -> JobRecord:
    """Mark the remaining steps cancelled and finish the job as cancelled."""
    with connect(settings) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE ai_job_step
                SET status = 'cancelled',
                    completed_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE job_id = %s AND status IN ('queued', 'running')
                """,
                (job_id,),
            )
    logger.info("job_finished job=%s outcome=cancelled", job_id)
    return _finish_job(settings, job_id, CANCELLED, "cancel_requested")


def _finish_job(
    settings: Settings, job_id: str, status: str, error_code: str | None
) -> JobRecord:
    with connect(settings) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE ai_job
                SET status = %s,
                    error_code = %s,
                    completed_at = CURRENT_TIMESTAMP,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s AND status IN ('queued', 'running', 'clarification')
                """,
                (status, error_code, job_id),
            )
    record = _load_job_unscoped(settings, job_id)
    if record is None:  # pragma: no cover - defensive
        raise JobNotFound("job not found")
    return record


def _load_steps(cursor: Any, job_id: str) -> tuple[JobStepRecord, ...]:
    cursor.execute(
        """
        SELECT step_key, ordinal, status, attempt_count, error_code
        FROM ai_job_step
        WHERE job_id = %s
        ORDER BY ordinal
        """,
        (job_id,),
    )
    return tuple(
        JobStepRecord(
            step_key=str(row["step_key"]),
            ordinal=int(row["ordinal"]),
            status=str(row["status"]),
            attempt_count=int(row["attempt_count"]),
            error_code=row.get("error_code"),
        )
        for row in cursor.fetchall()
    )


def _job_from_row(row: dict[str, Any], steps: tuple[JobStepRecord, ...]) -> JobRecord:
    return JobRecord(
        id=str(row["id"]),
        conversation_id=str(row["conversation_id"]),
        job_type=str(row["job_type"]),
        status=str(row["status"]),
        attempt_count=int(row["attempt_count"]),
        max_attempts=int(row["max_attempts"]),
        context_version=int(row.get("context_version") or 1),
        error_code=row.get("error_code"),
        cancel_requested=bool(row.get("cancel_requested_at")),
        created_at=row.get("created_at"),
        started_at=row.get("started_at"),
        completed_at=row.get("completed_at"),
        steps=steps,
    )


def _replace(record: JobRecord, **changes: Any) -> JobRecord:
    values = {
        "id": record.id,
        "conversation_id": record.conversation_id,
        "job_type": record.job_type,
        "status": record.status,
        "attempt_count": record.attempt_count,
        "max_attempts": record.max_attempts,
        "context_version": record.context_version,
        "error_code": record.error_code,
        "cancel_requested": record.cancel_requested,
        "created_at": record.created_at,
        "started_at": record.started_at,
        "completed_at": record.completed_at,
        "replayed": record.replayed,
        "steps": record.steps,
    }
    values.update(changes)
    return JobRecord(**values)


def _sealable(request: dict[str, Any]) -> str:
    """Serialize the request, rejecting anything too large to store."""
    try:
        payload = json.dumps(request, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise JobInputError("request must be JSON serializable") from exc
    if len(payload.encode("utf-8")) > MAX_REQUEST_BYTES:
        raise JobInputError("request is too large")
    return payload


def _clean_steps(steps: tuple[str, ...]) -> tuple[str, ...]:
    if not steps:
        return ("run",)
    if len(steps) > MAX_STEPS:
        raise JobInputError(f"A job accepts at most {MAX_STEPS} steps")
    cleaned: list[str] = []
    for raw in steps:
        key = str(raw or "").strip()
        if not key or len(key) > MAX_STEP_KEY_CHARS:
            raise JobInputError(
                f"step keys must be 1-{MAX_STEP_KEY_CHARS} characters"
            )
        if key in cleaned:
            raise JobInputError("step keys must be unique")
        cleaned.append(key)
    return tuple(cleaned)


def _error_code(exc: BaseException) -> str:
    code = getattr(exc, "code", None) or type(exc).__name__
    return str(code)[:MAX_ERROR_CODE_CHARS]


def _reference(output: Any) -> str | None:
    """Step output is summarized as an external reference, never inlined."""
    if not isinstance(output, dict):
        return None
    reference = output.get("reference")
    if reference is None:
        return None
    try:
        return str(uuid.UUID(str(reference)))
    except (ValueError, TypeError, AttributeError):
        return None


def _tenant_id(cursor: Any, tenant_key: str) -> str:
    cursor.execute("SELECT id FROM ai_tenant WHERE key = %s", (tenant_key,))
    row = cursor.fetchone()
    if row is None:
        raise JobInputError(f"Unknown tenant {tenant_key}")
    return str(row["id"])


def _assert_conversation(
    cursor: Any, conversation_id: str, tenant_id: str, superset_user_id: int
) -> None:
    cursor.execute(
        """
        SELECT id
        FROM ai_conversation
        WHERE id = %s
          AND tenant_id = %s
          AND superset_user_id = %s
          AND deleted_at IS NULL
        """,
        (conversation_id, tenant_id, int(superset_user_id)),
    )
    if cursor.fetchone() is None:
        raise JobNotFound("conversation not found")


def is_job_id(value: str) -> bool:
    """Is this a syntactically valid job identifier?"""
    try:
        uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def _isoformat(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
