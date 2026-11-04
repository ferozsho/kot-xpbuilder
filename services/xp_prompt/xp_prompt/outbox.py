"""Durable delivery for side effects that must not be lost (P9-F06).

Recording usage in the request handler is fine until the write fails: the answer
still reaches the user, the ledger row does not exist, and nobody finds out. An
outbox turns that into a retryable problem instead of a silent gap.

The flow, and why each step is where it is:

* **emit commits first.** The event is written before anything is applied, so a
  process that dies mid-flight leaves work behind rather than losing it.
* **claim is exclusive.** ``FOR UPDATE SKIP LOCKED`` means two workers draining at
  once take different events instead of both applying the same one.
* **apply and mark happen in one transaction.** If the consumer raises, the
  transaction rolls back and the event stays claimable — an applied event is never
  left in ``delivering``.
* **failure is recorded in its own transaction.** Otherwise the rollback that saved
  us would also erase the evidence that a delivery was attempted.
* **dead-letter is a bounded, visible end.** After :data:`MAX_ATTEMPTS` the event
  moves to ``dead_letter`` with its last error, so an operator sees the failure in
  the reconciliation report instead of a queue that grows silently.

Consumers must be idempotent, because at-least-once is the promise here, not
exactly-once.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from xp_prompt.config import Settings
from xp_prompt.db import connect

logger = logging.getLogger(__name__)

PENDING = "pending"
DELIVERING = "delivering"
DELIVERED = "delivered"
DEAD_LETTER = "dead_letter"
STATUSES: tuple[str, ...] = (PENDING, DELIVERING, DELIVERED, DEAD_LETTER)

MAX_ATTEMPTS = 5
BASE_BACKOFF_SECONDS = 2
MAX_BACKOFF_SECONDS = 300
DEFAULT_BATCH = 25
MAX_BATCH = 200
MAX_ERROR_CHARS = 1_000
MAX_AGGREGATE_CHARS = 64
MAX_EVENT_CHARS = 128


class OutboxError(ValueError):
    """A refused outbox operation; ``code`` is stable and safe to show."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class Event:
    id: str
    aggregate_type: str
    aggregate_id: str
    event_type: str
    payload: Any
    status: str
    attempt_count: int
    last_error: str | None = None
    available_at: datetime | None = None
    delivered_at: datetime | None = None

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
            "event_type": self.event_type,
            "status": self.status,
            "attempt_count": self.attempt_count,
            "last_error": self.last_error,
            "available_at": _isoformat(self.available_at),
            "delivered_at": _isoformat(self.delivered_at),
        }


def emit(
    settings: Settings,
    *,
    tenant_id: Any,
    aggregate_type: str,
    aggregate_id: Any = None,
    event_type: str,
    payload: Mapping[str, Any],
    available_at: datetime | None = None,
) -> str:
    """Store one event, durably, before anyone tries to act on it."""
    kind = _text(aggregate_type, MAX_AGGREGATE_CHARS)
    name = _text(event_type, MAX_EVENT_CHARS)
    if not tenant_id:
        raise OutboxError("tenant_required", "an event must belong to a tenant")
    if not kind:
        raise OutboxError("invalid_event", "an event needs an aggregate type")
    if not name:
        raise OutboxError("invalid_event", "an event needs a type")
    if not isinstance(payload, Mapping):
        raise OutboxError("invalid_event", "an event payload must be an object")

    identifier = str(aggregate_id or uuid.uuid4())
    with connect(settings) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO ai_outbox_event (
                tenant_id, aggregate_type, aggregate_id, event_type, payload,
                available_at
            )
            VALUES (%s, %s, %s, %s, %s, COALESCE(%s, CURRENT_TIMESTAMP))
            RETURNING id
            """,
            (str(tenant_id), kind, identifier, name, _json(payload), available_at),
        )
        event_id = str(cursor.fetchone()["id"])
    logger.info("outbox_emit event=%s type=%s aggregate=%s", event_id, name, kind)
    return event_id


def claim(
    settings: Settings,
    *,
    limit: int = DEFAULT_BATCH,
    event_type: str | None = None,
    event_id: str | None = None,
) -> list[Event]:
    """Take up to ``limit`` deliverable events, exclusively."""
    bounded = max(1, min(int(limit), MAX_BATCH))
    with connect(settings) as connection, connection.cursor() as cursor:
        return [_event(row) for row in _claim(cursor, bounded, event_type, event_id)]


def deliver(
    settings: Settings,
    *,
    handlers: Mapping[str, Callable[[Any, Any], Any]],
    limit: int = DEFAULT_BATCH,
    event_type: str | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    """Apply a batch of events, one transaction each, and report what happened.

    One transaction per event on purpose: a single poisonous event must not roll
    back a batch of good ones.
    """
    events = claim(
        settings, limit=limit, event_type=event_type, event_id=event_id
    )
    delivered = duplicates = failed = 0
    for event in events:
        handler = handlers.get(event.event_type)
        if handler is None:
            failed += 1
            record_failure(
                settings,
                event.id,
                f"no handler for {event.event_type}",
                attempts=event.attempt_count,
            )
            continue
        try:
            with connect(settings) as connection, connection.cursor() as cursor:
                outcome = handler(cursor, event.payload)
                mark_delivered(cursor, event.id)
            if outcome is False:
                duplicates += 1
            else:
                delivered += 1
        except Exception as exc:  # pylint: disable=broad-except
            failed += 1
            record_failure(
                settings, event.id, f"{type(exc).__name__}: {exc}",
                attempts=event.attempt_count,
            )
    return {
        "claimed": len(events),
        "delivered": delivered,
        "duplicates": duplicates,
        "failed": failed,
    }


def mark_delivered(cursor: Any, event_id: Any) -> None:
    cursor.execute(
        """
        UPDATE ai_outbox_event
        SET status = 'delivered',
            delivered_at = CURRENT_TIMESTAMP,
            updated_at = CURRENT_TIMESTAMP,
            last_error = NULL
        WHERE id = %s
        """,
        (str(event_id),),
    )


def record_failure(
    settings: Settings,
    event_id: Any,
    error: str,
    *,
    attempts: int | None = None,
) -> str:
    """Count a failed delivery and schedule the retry, or dead-letter it.

    Runs in its own transaction because the transaction that failed has already
    rolled back — the evidence has to be written separately or it dies with it.
    """
    with connect(settings) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT attempt_count FROM ai_outbox_event WHERE id = %s
            """,
            (str(event_id),),
        )
        row = cursor.fetchone()
        current = int(attempts if attempts is not None else (row or {}).get(
            "attempt_count", 0
        ))
        count = current + 1
        exhausted = count >= MAX_ATTEMPTS
        cursor.execute(
            """
            UPDATE ai_outbox_event
            SET status = %s,
                attempt_count = %s,
                last_error = %s,
                available_at = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
            RETURNING status
            """,
            (
                DEAD_LETTER if exhausted else PENDING,
                count,
                _text(error, MAX_ERROR_CHARS),
                datetime.now(UTC) + _backoff(count),
                str(event_id),
            ),
        )
        updated = (cursor.fetchone() or {}).get("status", PENDING)
    if exhausted:
        logger.error(
            "outbox_dead_letter event=%s attempts=%d error=%s",
            event_id,
            count,
            _text(error, 200),
        )
    else:
        logger.warning(
            "outbox_retry event=%s attempt=%d error=%s",
            event_id,
            count,
            _text(error, 200),
        )
    return str(updated)


def pending(settings: Settings, *, tenant_id: Any = None) -> dict[str, int]:
    """How much work is waiting, by status — the operator's first question."""
    where = "WHERE tenant_id = %s" if tenant_id else ""
    params: tuple[Any, ...] = (str(tenant_id),) if tenant_id else ()
    with connect(settings) as connection, connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT status, COUNT(*) AS events
            FROM ai_outbox_event
            {where}
            GROUP BY status
            """,
            params,
        )
        rows = cursor.fetchall()
    counts = {status: 0 for status in STATUSES}
    for row in rows:
        counts[str(row["status"])] = int(row["events"])
    return counts


def dead_letters(
    settings: Settings, *, tenant_id: Any = None, limit: int = DEFAULT_BATCH
) -> list[Event]:
    """The events that gave up, with the error that stopped them."""
    bounded = max(1, min(int(limit), MAX_BATCH))
    clauses = ["status = 'dead_letter'"]
    params: list[Any] = []
    if tenant_id:
        clauses.append("tenant_id = %s")
        params.append(str(tenant_id))
    with connect(settings) as connection, connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT id, aggregate_type, aggregate_id, event_type, payload, status,
                   attempt_count, last_error, available_at, delivered_at
            FROM ai_outbox_event
            WHERE {' AND '.join(clauses)}
            ORDER BY updated_at DESC
            LIMIT %s
            """,
            (*params, bounded),
        )
        rows = cursor.fetchall()
    return [_event(row) for row in rows]


def requeue(settings: Settings, event_id: Any) -> Event:
    """Put a dead-lettered event back in the queue after the cause is fixed."""
    with connect(settings) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE ai_outbox_event
            SET status = 'pending',
                attempt_count = 0,
                available_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s AND status = 'dead_letter'
            RETURNING id, aggregate_type, aggregate_id, event_type, payload,
                      status, attempt_count, last_error, available_at, delivered_at
            """,
            (str(event_id),),
        )
        row = cursor.fetchone()
    if row is None:
        raise OutboxError("not_dead_letter", "event is not dead-lettered")
    logger.info("outbox_requeue event=%s", event_id)
    return _event(row)


def _claim(
    cursor: Any,
    limit: int,
    event_type: str | None,
    event_id: str | None,
) -> list[dict[str, Any]]:
    """Take exclusive ownership of deliverable events.

    ``SKIP LOCKED`` is what makes two drainers safe: the second one steps over the
    rows the first is holding instead of blocking on them.
    """
    clauses = ["status = 'pending'", "available_at <= CURRENT_TIMESTAMP"]
    params: list[Any] = []
    if event_type:
        clauses.append("event_type = %s")
        params.append(event_type)
    if event_id:
        clauses.append("id = %s")
        params.append(str(event_id))
    where = " AND ".join(clauses)
    cursor.execute(
        f"""
        UPDATE ai_outbox_event
        SET status = 'delivering', updated_at = CURRENT_TIMESTAMP
        WHERE id IN (
            SELECT id FROM ai_outbox_event
            WHERE {where}
            ORDER BY available_at
            LIMIT %s
            FOR UPDATE SKIP LOCKED
        )
        RETURNING id, aggregate_type, aggregate_id, event_type, payload, status,
                  attempt_count, last_error, available_at, delivered_at
        """,
        # Clamped here, not only by the caller: an unbounded claim is exactly the
        # query that takes the table lock a busy deployment cannot afford.
        (*params, max(1, min(int(limit), MAX_BATCH))),
    )
    return cursor.fetchall()


def _backoff(attempt: int) -> timedelta:
    """Bounded exponential backoff, so a broken consumer cannot spin."""
    seconds = BASE_BACKOFF_SECONDS * (2 ** max(0, attempt - 1))
    return timedelta(seconds=min(seconds, MAX_BACKOFF_SECONDS))


def _event(row: Mapping[str, Any]) -> Event:
    return Event(
        id=str(row["id"]),
        aggregate_type=row["aggregate_type"],
        aggregate_id=str(row["aggregate_id"]),
        event_type=row["event_type"],
        payload=_load(row.get("payload"), {}),
        status=row["status"],
        attempt_count=int(row.get("attempt_count") or 0),
        last_error=row.get("last_error"),
        available_at=row.get("available_at"),
        delivered_at=row.get("delivered_at"),
    )


def _json(value: Any) -> str:
    import json

    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _load(value: Any, default: Any) -> Any:
    import json

    if value is None:
        return default
    if isinstance(value, str | bytes):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return default
    return value


def _text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _isoformat(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
