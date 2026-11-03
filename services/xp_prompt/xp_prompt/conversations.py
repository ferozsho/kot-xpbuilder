"""Conversation and message persistence for the XP Prompt workspace.

Phase 2 (P2-F02) owns the conversational surface. This module is the only place
that reads or writes ``ai_conversation`` / ``ai_message``, and it enforces two
rules on every path:

* **ownership** — a conversation is addressed by ``(tenant, superset_user_id)``
  as well as its id, so knowing an id is never enough to read or extend someone
  else's chat;
* **redaction** — message bodies are sealed with the same :class:`SecretBox` used
  for provider keys, and only a hash plus a short redacted excerpt is queryable,
  so the database never holds a plaintext transcript.

The module deliberately does not know about Superset; the service API resolves
the caller's tenant and user before calling in.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from xp_prompt.config import Settings
from xp_prompt.db import connect
from xp_prompt.security import SecretBox

#: Longest accepted user message. A provider request is truncated far earlier,
#: so anything beyond this is a client defect rather than a prompt.
MAX_MESSAGE_CHARS = 8_000
MAX_TITLE_CHARS = 255
MAX_EXCERPT_CHARS = 280
DEFAULT_LIST_LIMIT = 25
MAX_LIST_LIMIT = 100
ARCHIVED_STATUS = "archived"
ACTIVE_STATUS = "active"


class ConversationNotFound(LookupError):
    """No conversation with that id belongs to this tenant/user pair."""


class ConversationInputError(ValueError):
    """A rejected conversation or message payload (safe to return to the UI)."""


class ContextVersionConflict(RuntimeError):
    """The caller's context version is stale; it must reload the conversation.

    Raised instead of silently replaying a context the caller has not seen, so a
    reset performed in another tab can never be undone by a slow request.
    """


@dataclass(frozen=True, slots=True)
class ConversationRecord:
    id: str
    tenant_key: str
    superset_user_id: int
    title: str
    status: str
    context_version: int
    last_message_at: datetime | None
    created_at: datetime | None
    updated_at: datetime | None

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tenant": self.tenant_key,
            "superset_user_id": self.superset_user_id,
            "title": self.title,
            "status": self.status,
            "context_version": self.context_version,
            "last_message_at": _isoformat(self.last_message_at),
            "created_at": _isoformat(self.created_at),
            "updated_at": _isoformat(self.updated_at),
        }


@dataclass(frozen=True, slots=True)
class MessageRecord:
    id: str
    conversation_id: str
    role: str
    content: str | None
    structured_content: dict[str, Any]
    created_at: datetime | None
    #: Context version this message belongs to; only the conversation's current
    #: version is replayed to a provider.
    context_version: int = 1

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "conversation_id": self.conversation_id,
            "role": self.role,
            "content": self.content,
            "structured_content": dict(self.structured_content),
            "context_version": self.context_version,
            "created_at": _isoformat(self.created_at),
        }


def create_conversation(
    settings: Settings,
    *,
    tenant_key: str,
    superset_user_id: int,
    title: str | None = None,
) -> ConversationRecord:
    """Create an empty conversation owned by one Superset user."""
    clean_title = _clean_title(title)
    owner = _clean_user_id(superset_user_id)
    with connect(settings) as connection:
        with connection.cursor() as cursor:
            tenant_id = _tenant_id(cursor, tenant_key)
            cursor.execute(
                """
                INSERT INTO ai_conversation (tenant_id, superset_user_id, title)
                VALUES (%s, %s, %s)
                RETURNING id
                """,
                (tenant_id, owner, clean_title),
            )
            conversation_id = str(cursor.fetchone()["id"])
    record = get_conversation(
        settings,
        conversation_id,
        tenant_key=tenant_key,
        superset_user_id=owner,
    )
    if record is None:  # pragma: no cover - defensive
        raise RuntimeError("conversation was created but cannot be read back")
    return record


def list_conversations(
    settings: Settings,
    *,
    tenant_key: str,
    superset_user_id: int,
    limit: int = DEFAULT_LIST_LIMIT,
    include_archived: bool = False,
) -> list[ConversationRecord]:
    """Most recently used conversations for one user, newest first."""
    owner = _clean_user_id(superset_user_id)
    bounded_limit = max(1, min(int(limit), MAX_LIST_LIMIT))
    statuses = [ACTIVE_STATUS, ARCHIVED_STATUS] if include_archived else [ACTIVE_STATUS]
    with connect(settings) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT c.*, t.key AS tenant_key
                FROM ai_conversation c
                JOIN ai_tenant t ON t.id = c.tenant_id
                WHERE t.key = %s
                  AND c.superset_user_id = %s
                  AND c.deleted_at IS NULL
                  AND c.status = ANY(%s)
                ORDER BY COALESCE(c.last_message_at, c.created_at) DESC
                LIMIT %s
                """,
                (tenant_key, owner, statuses, bounded_limit),
            )
            return [_conversation_from_row(row) for row in cursor.fetchall()]


def get_conversation(
    settings: Settings,
    conversation_id: str,
    *,
    tenant_key: str,
    superset_user_id: int,
) -> ConversationRecord | None:
    """Load one conversation, scoped to its owner."""
    owner = _clean_user_id(superset_user_id)
    with connect(settings) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT c.*, t.key AS tenant_key
                FROM ai_conversation c
                JOIN ai_tenant t ON t.id = c.tenant_id
                WHERE c.id = %s
                  AND t.key = %s
                  AND c.superset_user_id = %s
                  AND c.deleted_at IS NULL
                """,
                (conversation_id, tenant_key, owner),
            )
            row = cursor.fetchone()
    return _conversation_from_row(row) if row else None


def list_messages(
    settings: Settings,
    conversation_id: str,
    *,
    tenant_key: str,
    superset_user_id: int,
    limit: int = 50,
    context_version: int | None = None,
) -> list[MessageRecord]:
    """Messages of one conversation in creation order.

    ``context_version`` restricts the result to one context generation: the turn
    passes the conversation's current version, so messages from before an
    explicit reset stay stored (and visible) without being replayed to a model.
    """
    bounded_limit = max(1, min(int(limit), 200))
    with connect(settings) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT m.*
                FROM ai_message m
                JOIN ai_conversation c ON c.id = m.conversation_id
                JOIN ai_tenant t ON t.id = m.tenant_id
                WHERE m.conversation_id = %s
                  AND t.key = %s
                  AND c.superset_user_id = %s
                  AND (%s::int IS NULL OR m.context_version = %s)
                ORDER BY m.created_at ASC
                LIMIT %s
                """,
                (
                    conversation_id,
                    tenant_key,
                    superset_user_id,
                    context_version,
                    context_version,
                    bounded_limit,
                ),
            )
            rows = cursor.fetchall()
    return [_message_from_row(row, settings) for row in rows]


def append_message(
    settings: Settings,
    conversation_id: str,
    *,
    tenant_key: str,
    superset_user_id: int,
    role: str,
    content: str,
    structured_content: dict[str, Any] | None = None,
) -> MessageRecord:
    """Append one message, sealing the body and refreshing the conversation.

    Raises :class:`ConversationNotFound` when the conversation does not belong
    to this tenant/user pair, so a caller can never write into another chat.
    """
    if role not in {"user", "assistant", "tool", "system"}:
        raise ConversationInputError("Unsupported message role")
    text = (content or "").strip()
    if not text:
        raise ConversationInputError("Message content is required")
    if len(text) > MAX_MESSAGE_CHARS:
        raise ConversationInputError(
            f"Message content must be at most {MAX_MESSAGE_CHARS} characters"
        )

    box = SecretBox.from_root_secret(settings.service_secret)
    with connect(settings) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT c.id, c.tenant_id, c.context_version
                FROM ai_conversation c
                JOIN ai_tenant t ON t.id = c.tenant_id
                WHERE c.id = %s
                  AND t.key = %s
                  AND c.superset_user_id = %s
                  AND c.deleted_at IS NULL
                """,
                (conversation_id, tenant_key, superset_user_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise ConversationNotFound("conversation not found")
            tenant_id = row["tenant_id"]
            context_version = int(row.get("context_version") or 1)

            cursor.execute(
                """
                INSERT INTO ai_message (
                    tenant_id, conversation_id, role, content_ciphertext,
                    content_hash, redacted_excerpt, structured_content,
                    context_version
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    tenant_id,
                    conversation_id,
                    role,
                    box.encrypt(text).encode("utf-8"),
                    _content_hash(text),
                    redact(text),
                    _json(structured_content or {}),
                    context_version,
                ),
            )
            message_id = str(cursor.fetchone()["id"])
            cursor.execute(
                """
                UPDATE ai_conversation
                SET last_message_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (conversation_id,),
            )
    return MessageRecord(
        id=message_id,
        conversation_id=conversation_id,
        role=role,
        content=text,
        structured_content=dict(structured_content or {}),
        created_at=None,
        context_version=context_version,
    )


def reset_context(
    settings: Settings,
    conversation_id: str,
    *,
    tenant_key: str,
    superset_user_id: int,
    expected_version: int | None = None,
) -> ConversationRecord:
    """Start a fresh context generation without deleting the transcript.

    The version bump is conditional on ``expected_version`` (when supplied), so
    a request prepared against a context that has since been reset is rejected
    with :class:`ContextVersionConflict` instead of silently resetting again.
    Older messages stay stored and visible; the turn stops replaying them.
    """
    record = get_conversation(
        settings,
        conversation_id,
        tenant_key=tenant_key,
        superset_user_id=superset_user_id,
    )
    if record is None:
        raise ConversationNotFound("conversation not found")
    if expected_version is not None and int(expected_version) != record.context_version:
        raise ContextVersionConflict(
            "conversation context changed; reload the conversation"
        )

    with connect(settings) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE ai_conversation c
                SET context_version = c.context_version + 1,
                    updated_at = CURRENT_TIMESTAMP
                FROM ai_tenant t
                WHERE c.tenant_id = t.id
                  AND t.key = %s
                  AND c.id = %s
                  AND c.superset_user_id = %s
                  AND c.deleted_at IS NULL
                  AND c.context_version = %s
                RETURNING c.context_version
                """,
                (tenant_key, conversation_id, record.superset_user_id, record.context_version),
            )
            row = cursor.fetchone()
    if row is None:  # pragma: no cover - only reachable under a concurrent reset
        raise ContextVersionConflict(
            "conversation context changed; reload the conversation"
        )

    refreshed = get_conversation(
        settings,
        conversation_id,
        tenant_key=tenant_key,
        superset_user_id=superset_user_id,
    )
    if refreshed is None:  # pragma: no cover - defensive
        raise ConversationNotFound("conversation not found")
    return refreshed


def archive_conversation(
    settings: Settings,
    conversation_id: str,
    *,
    tenant_key: str,
    superset_user_id: int,
) -> bool:
    """Archive a conversation; returns ``False`` when the owner has no such row."""
    with connect(settings) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE ai_conversation c
                SET status = %s, updated_at = CURRENT_TIMESTAMP
                FROM ai_tenant t
                WHERE c.tenant_id = t.id
                  AND c.id = %s
                  AND t.key = %s
                  AND c.superset_user_id = %s
                  AND c.deleted_at IS NULL
                """,
                (ARCHIVED_STATUS, conversation_id, tenant_key, superset_user_id),
            )
            return cursor.rowcount > 0


def redact(text: str) -> str:
    """Return a stored-for-search excerpt with obvious secrets removed.

    The full body lives encrypted; this column exists so an operator can eyeball
    a conversation without decrypting it, so anything that looks like a
    credential is stripped before it is written.
    """
    import re

    trimmed = " ".join((text or "").split())
    trimmed = re.sub(r"\b(sk|pk|AIza|ghp|xoxb)-?[A-Za-z0-9_\-]{8,}", "[redacted]", trimmed)
    trimmed = re.sub(
        r"(?i)\b(password|passwd|secret|token|api[_ -]?key)\s*[:=]\s*\S+",
        r"\1=[redacted]",
        trimmed,
    )
    if len(trimmed) > MAX_EXCERPT_CHARS:
        return trimmed[: MAX_EXCERPT_CHARS - 1] + "…"
    return trimmed


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clean_title(title: str | None) -> str:
    text = (title or "").strip() or "New chat"
    if len(text) > MAX_TITLE_CHARS:
        raise ConversationInputError(
            f"Title must be at most {MAX_TITLE_CHARS} characters"
        )
    return text


def _clean_user_id(value: int) -> int:
    try:
        owner = int(value)
    except (TypeError, ValueError) as exc:
        raise ConversationInputError("superset_user_id must be an integer") from exc
    if owner <= 0:
        raise ConversationInputError("superset_user_id must be positive")
    return owner


def _tenant_id(cursor: Any, tenant_key: str) -> str:
    cursor.execute("SELECT id FROM ai_tenant WHERE key = %s", (tenant_key,))
    row = cursor.fetchone()
    if row is None:
        raise ConversationInputError(f"Unknown tenant {tenant_key}")
    return str(row["id"])


def _conversation_from_row(row: dict[str, Any]) -> ConversationRecord:
    return ConversationRecord(
        id=str(row["id"]),
        tenant_key=str(row.get("tenant_key") or "default"),
        superset_user_id=int(row["superset_user_id"]),
        title=str(row["title"]),
        status=str(row["status"]),
        context_version=int(row["context_version"]),
        last_message_at=row.get("last_message_at"),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


def _message_from_row(row: dict[str, Any], settings: Settings) -> MessageRecord:
    """Decrypt a stored body; a body sealed with an old secret reads as absent."""
    content: str | None = None
    ciphertext = row.get("content_ciphertext")
    if ciphertext:
        try:
            box = SecretBox.from_root_secret(settings.service_secret)
            content = box.decrypt(bytes(ciphertext).decode("ascii"))
        except (RuntimeError, UnicodeDecodeError, ValueError):
            content = None
    structured = row.get("structured_content") or {}
    return MessageRecord(
        id=str(row["id"]),
        conversation_id=str(row["conversation_id"]),
        role=str(row["role"]),
        content=content,
        structured_content=dict(structured) if isinstance(structured, dict) else {},
        created_at=row.get("created_at"),
        context_version=int(row.get("context_version") or 1),
    )


def _json(value: dict[str, Any]) -> str:
    import json

    return json.dumps(value, separators=(",", ":"))


def _isoformat(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
