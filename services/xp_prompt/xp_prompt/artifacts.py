"""Artifact lifecycle for the XP Prompt workspace (P2-F07).

An *artifact* is a proposed Superset object: a dataset, a chart, or a dashboard.
Every type shares one lifecycle, deliberately, so there is no second hidden
pipeline for "full dashboards":

    draft ──confirm──> confirmed ──publish──> published
      │                    │
      └────discard─────────┴──> discarded

* **draft** — a producer (a job step in Phases 6–8) has described the object; the
  user has seen a proposal and nothing exists in Superset.
* **confirmed** — the user approved it. This is the only state a publisher may
  act on, which is what "a message alone cannot mutate Superset objects" means
  for artifacts (the job gate in ADR-0007 is the same rule at the runner level).
* **published** — a publisher created the real object and recorded its identity.
  Links are rendered from that identity (ADR-0007), never from a stored URL.
* **discarded** — the user declined, or the draft went stale. Terminal.

Payloads are sealed with :class:`SecretBox`: a dataset/chart specification can
contain SQL and column names, so the database must not hold it in plain text. The
payload is also bounded on the way in, so a proposal cannot be used as a dump
channel.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from xp_prompt.config import Settings
from xp_prompt.db import connect
from xp_prompt.security import SecretBox

logger = logging.getLogger(__name__)

DRAFT = "draft"
CONFIRMED = "confirmed"
PUBLISHED = "published"
DISCARDED = "discarded"

ARTIFACT_STATUSES: tuple[str, ...] = (DRAFT, CONFIRMED, PUBLISHED, DISCARDED)
ARTIFACT_TYPES: tuple[str, ...] = ("dataset", "chart", "dashboard")

MAX_TITLE_CHARS = 255
MAX_PAYLOAD_BYTES = 64 * 1024
MAX_OBJECT_ID_CHARS = 64
DEFAULT_LIST_LIMIT = 20
MAX_LIST_LIMIT = 100

#: Published object ids must match the link renderer's rule (ADR-0007): an id,
#: never a URL.
_SAFE_OBJECT_ID = re.compile(r"^[A-Za-z0-9_-]+$")


class ArtifactNotFound(LookupError):
    """No artifact with that id belongs to this tenant/user pair."""


class ArtifactInputError(ValueError):
    """A rejected artifact payload (safe to return to the UI)."""


class ArtifactStateError(RuntimeError):
    """The artifact is not in a state that allows the requested transition."""


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    id: str
    conversation_id: str
    artifact_type: str
    status: str
    title: str
    payload: dict[str, Any]
    published_object_type: str | None = None
    published_object_id: str | None = None
    job_id: str | None = None
    created_at: datetime | None = None
    confirmed_at: datetime | None = None
    published_at: datetime | None = None
    discarded_at: datetime | None = None

    @property
    def is_publishable(self) -> bool:
        return self.status == CONFIRMED

    @property
    def native_link(self) -> dict[str, str] | None:
        """The identity of the published object, for a `links` block (P2-F09)."""
        if self.status != PUBLISHED or not self.published_object_type:
            return None
        if not self.published_object_id:
            return None
        return {
            "entity_type": self.published_object_type,
            "entity_id": self.published_object_id,
        }

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "conversation_id": self.conversation_id,
            "job_id": self.job_id,
            "artifact_type": self.artifact_type,
            "status": self.status,
            "title": self.title,
            "payload": dict(self.payload),
            "published_object_type": self.published_object_type,
            "published_object_id": self.published_object_id,
            "native_link": self.native_link,
            "created_at": _isoformat(self.created_at),
            "confirmed_at": _isoformat(self.confirmed_at),
            "published_at": _isoformat(self.published_at),
            "discarded_at": _isoformat(self.discarded_at),
        }


def can_confirm(record: ArtifactRecord) -> bool:
    return record.status == DRAFT


def can_publish(record: ArtifactRecord) -> bool:
    return record.status == CONFIRMED


def can_discard(record: ArtifactRecord) -> bool:
    return record.status in {DRAFT, CONFIRMED}


def create_artifact(
    settings: Settings,
    *,
    conversation_id: str,
    tenant_key: str,
    superset_user_id: int,
    artifact_type: str,
    title: str,
    payload: dict[str, Any] | None = None,
    job_id: str | None = None,
) -> ArtifactRecord:
    """Record a draft proposal; nothing is created in Superset by this call."""
    kind = (artifact_type or "").strip().lower()
    if kind not in ARTIFACT_TYPES:
        raise ArtifactInputError(f"Unsupported artifact type: {artifact_type}")
    clean_title = " ".join((title or "").split())
    if len(clean_title) > MAX_TITLE_CHARS:
        raise ArtifactInputError(
            f"title must be at most {MAX_TITLE_CHARS} characters"
        )
    body = _sealable_payload(payload or {})
    box = SecretBox.from_root_secret(settings.service_secret)
    sealed = box.encrypt(body).encode("utf-8")

    with connect(settings) as connection:
        with connection.cursor() as cursor:
            tenant_id = _tenant_id(cursor, tenant_key)
            _assert_conversation(cursor, conversation_id, tenant_id, superset_user_id)
            cursor.execute(
                """
                INSERT INTO ai_artifact (
                    tenant_id, conversation_id, job_id, superset_user_id,
                    artifact_type, title, payload_ciphertext
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    tenant_id,
                    conversation_id,
                    job_id,
                    int(superset_user_id),
                    kind,
                    clean_title,
                    sealed,
                ),
            )
            artifact_id = str(cursor.fetchone()["id"])

    record = get_artifact(
        settings,
        artifact_id,
        tenant_key=tenant_key,
        superset_user_id=int(superset_user_id),
    )
    if record is None:  # pragma: no cover - defensive
        raise ArtifactInputError("artifact was created but cannot be read back")
    logger.info(
        "artifact_created artifact=%s type=%s conversation=%s", artifact_id, kind, conversation_id
    )
    return record


def get_artifact(
    settings: Settings,
    artifact_id: str,
    *,
    tenant_key: str,
    superset_user_id: int,
) -> ArtifactRecord | None:
    with connect(settings) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT a.*
                FROM ai_artifact a
                JOIN ai_tenant t ON t.id = a.tenant_id
                WHERE a.id = %s
                  AND t.key = %s
                  AND a.superset_user_id = %s
                """,
                (artifact_id, tenant_key, int(superset_user_id)),
            )
            row = cursor.fetchone()
    return _artifact_from_row(row, settings) if row else None


def list_artifacts(
    settings: Settings,
    conversation_id: str,
    *,
    tenant_key: str,
    superset_user_id: int,
    limit: int = DEFAULT_LIST_LIMIT,
) -> list[ArtifactRecord]:
    bounded = max(1, min(int(limit), MAX_LIST_LIMIT))
    with connect(settings) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT a.*
                FROM ai_artifact a
                JOIN ai_tenant t ON t.id = a.tenant_id
                WHERE a.conversation_id = %s
                  AND t.key = %s
                  AND a.superset_user_id = %s
                ORDER BY a.created_at DESC
                LIMIT %s
                """,
                (conversation_id, tenant_key, int(superset_user_id), bounded),
            )
            rows = cursor.fetchall()
    return [_artifact_from_row(row, settings) for row in rows]


def confirm_artifact(
    settings: Settings,
    artifact_id: str,
    *,
    tenant_key: str,
    superset_user_id: int,
) -> ArtifactRecord:
    """Approve a draft; only a publisher may act on the result."""
    return _transition(
        settings,
        artifact_id,
        tenant_key=tenant_key,
        superset_user_id=superset_user_id,
        sql="""
            UPDATE ai_artifact a
            SET status = 'confirmed',
                confirmed_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            FROM ai_tenant t
            WHERE a.tenant_id = t.id
              AND t.key = %s
              AND a.id = %s
              AND a.superset_user_id = %s
              AND a.status = 'draft'
            RETURNING a.id
            """,
        allowed=can_confirm,
        error="artifact is not a draft",
    )


def discard_artifact(
    settings: Settings,
    artifact_id: str,
    *,
    tenant_key: str,
    superset_user_id: int,
) -> ArtifactRecord:
    """Decline a draft or a confirmed artifact; it can never be published after."""
    return _transition(
        settings,
        artifact_id,
        tenant_key=tenant_key,
        superset_user_id=superset_user_id,
        sql="""
            UPDATE ai_artifact a
            SET status = 'discarded',
                discarded_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            FROM ai_tenant t
            WHERE a.tenant_id = t.id
              AND t.key = %s
              AND a.id = %s
              AND a.superset_user_id = %s
              AND a.status IN ('draft', 'confirmed')
            RETURNING a.id
            """,
        allowed=can_discard,
        error="artifact cannot be discarded",
    )


def publish_artifact(
    settings: Settings,
    artifact_id: str,
    *,
    tenant_key: str,
    superset_user_id: int,
    object_type: str,
    object_id: str,
) -> ArtifactRecord:
    """Record the object a publisher created (Phases 6–8 call this).

    Only a confirmed artifact can be published, and the identity must be a slug or
    integer id that the link renderer accepts — a publisher cannot slip a URL in.
    """
    kind = (object_type or "").strip().lower()
    if kind not in ARTIFACT_TYPES:
        raise ArtifactInputError(f"Unsupported object type: {object_type}")
    identifier = str(object_id or "").strip()
    if not identifier or len(identifier) > MAX_OBJECT_ID_CHARS:
        raise ArtifactInputError("object_id must be 1-64 characters")
    if not _SAFE_OBJECT_ID.match(identifier):
        raise ArtifactInputError("object_id must be alphanumeric, dash, or underscore")

    return _transition(
        settings,
        artifact_id,
        tenant_key=tenant_key,
        superset_user_id=superset_user_id,
        sql="""
            UPDATE ai_artifact a
            SET status = 'published',
                published_object_type = %s,
                published_object_id = %s,
                published_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            FROM ai_tenant t
            WHERE a.tenant_id = t.id
              AND t.key = %s
              AND a.id = %s
              AND a.superset_user_id = %s
              AND a.status = 'confirmed'
            RETURNING a.id
            """,
        allowed=can_publish,
        error="artifact is not confirmed",
        extra_params=(kind, identifier),
    )


def _transition(
    settings: Settings,
    artifact_id: str,
    *,
    tenant_key: str,
    superset_user_id: int,
    sql: str,
    allowed,
    error: str,
    extra_params: tuple[Any, ...] = (),
) -> ArtifactRecord:
    """Run one guarded transition, distinguishing 404 from 409."""
    current = get_artifact(
        settings,
        artifact_id,
        tenant_key=tenant_key,
        superset_user_id=superset_user_id,
    )
    if current is None:
        raise ArtifactNotFound("artifact not found")
    if not allowed(current):
        raise ArtifactStateError(error)

    params = (*extra_params, tenant_key, artifact_id, int(superset_user_id))
    with connect(settings) as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            updated = cursor.fetchone() is not None
    if not updated:  # pragma: no cover - only reachable under a concurrent change
        raise ArtifactStateError(error)

    record = get_artifact(
        settings,
        artifact_id,
        tenant_key=tenant_key,
        superset_user_id=int(superset_user_id),
    )
    if record is None:  # pragma: no cover - defensive
        raise ArtifactNotFound("artifact not found")
    logger.info(
        "artifact_transition artifact=%s status=%s", artifact_id, record.status
    )
    return record


def _sealable_payload(payload: dict[str, Any]) -> str:
    if not isinstance(payload, dict):
        raise ArtifactInputError("payload must be an object")
    try:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ArtifactInputError("payload must be JSON serializable") from exc
    if len(body.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise ArtifactInputError("payload is too large")
    return body


def _artifact_from_row(row: dict[str, Any], settings: Settings) -> ArtifactRecord:
    payload: dict[str, Any] = {}
    ciphertext = row.get("payload_ciphertext")
    if ciphertext:
        try:
            box = SecretBox.from_root_secret(settings.service_secret)
            decoded = box.decrypt(bytes(ciphertext).decode("ascii"))
            value = json.loads(decoded)
            payload = value if isinstance(value, dict) else {}
        except (RuntimeError, ValueError, UnicodeDecodeError):
            # A rotated root secret must not hide the record itself.
            payload = {}
    return ArtifactRecord(
        id=str(row["id"]),
        conversation_id=str(row["conversation_id"]),
        artifact_type=str(row["artifact_type"]),
        status=str(row["status"]),
        title=str(row["title"]),
        payload=payload,
        published_object_type=row.get("published_object_type"),
        published_object_id=row.get("published_object_id"),
        job_id=str(row["job_id"]) if row.get("job_id") else None,
        created_at=row.get("created_at"),
        confirmed_at=row.get("confirmed_at"),
        published_at=row.get("published_at"),
        discarded_at=row.get("discarded_at"),
    )


def _tenant_id(cursor: Any, tenant_key: str) -> str:
    cursor.execute("SELECT id FROM ai_tenant WHERE key = %s", (tenant_key,))
    row = cursor.fetchone()
    if row is None:
        raise ArtifactInputError(f"Unknown tenant {tenant_key}")
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
        raise ArtifactNotFound("conversation not found")


def is_artifact_id(value: str) -> bool:
    import uuid

    try:
        uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def _isoformat(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
