"""SQL turn audit trail (P4-F12).

One row per SQL turn — including the refusals — so an operator can answer "what did
the assistant try to run, for whom, and what stopped it" without reading anyone's
questions.

Design rules:

* **content is sealed.** The statement is stored with :class:`SecretBox` and a
  SHA-256 fingerprint is what is queryable, so "the same query was attempted twice"
  is answerable without a database dump becoming a catalogue of tenant data.
* **the statement is hidden by default.** :meth:`AuditRecord.to_public_dict` omits
  the SQL unless the caller asked for it *and* the deployment enabled diagnostics
  (``XP_PROMPT_ALLOW_SQL_DIAGNOSTICS``). Redaction is decided here, once, instead of
  in every consumer.
* **bounding is part of the contract.** Every free-form field is length-capped and
  the JSON lists are size-capped: an audit row must not become the place where a
  caller stores a document.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from xp_prompt.config import Settings
from xp_prompt.db import connect
from xp_prompt.security import SecretBox

logger = logging.getLogger(__name__)

#: A turn ends in exactly one of these.
OUTCOMES: tuple[str, ...] = ("answered", "refused", "failed")
#: Where it ended. ``guard`` is a policy refusal, ``execute`` a database failure.
STAGES: tuple[str, ...] = (
    "candidate",
    "guard",
    "plan",
    "execute",
    "result",
    "store",
)
#: Whether the statement was reused, regenerated, or the cache was unavailable.
CACHE_STATUSES: tuple[str, ...] = ("hit", "miss", "bypass", "disabled")

MAX_CODE_CHARS = 64
MAX_CORRELATION_CHARS = 64
MAX_INTENT_CHARS = 32
MAX_DIALECT_CHARS = 32
MAX_SQL_CHARS = 20_000
MAX_TABLES = 50
MAX_TABLE_CHARS = 128
MAX_LIST_ITEMS = 20
MAX_ITEM_CHARS = 64
DEFAULT_LIST_LIMIT = 20
MAX_LIST_LIMIT = 100


class AuditInputError(ValueError):
    """A rejected audit payload (safe to return to the caller)."""


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """One stored turn. ``sql`` is only populated when it was requested and allowed."""

    id: str
    conversation_id: str
    superset_user_id: int
    correlation_id: str
    intent: str
    outcome: str
    stage: str
    code: str
    dialect: str
    tables: tuple[str, ...] = ()
    inlined: tuple[str, ...] = ()
    rls_applied: tuple[str, ...] = ()
    plan_source: str | None = None
    plan_rows: int | None = None
    plan_cost: float | None = None
    plan_nodes: int | None = None
    plan_code: str | None = None
    duration_ms: int | None = None
    row_count: int | None = None
    returned: int | None = None
    truncated: bool = False
    sql_sha256: str = ""
    #: ``hit``/``miss``/``bypass``/``disabled`` — how the statement was obtained.
    cache_status: str = ""
    sql: str | None = field(default=None, repr=False)
    created_at: datetime | None = None

    def to_public_dict(self, *, include_sql: bool = False) -> dict[str, Any]:
        """Metadata always; the statement only on an explicit, allowed request."""
        payload: dict[str, Any] = {
            "id": self.id,
            "conversation_id": self.conversation_id,
            "superset_user_id": self.superset_user_id,
            "correlation_id": self.correlation_id,
            "intent": self.intent,
            "outcome": self.outcome,
            "stage": self.stage,
            "code": self.code,
            "dialect": self.dialect,
            "tables": list(self.tables),
            "inlined": list(self.inlined),
            "rls_applied": list(self.rls_applied),
            "plan": {
                "source": self.plan_source,
                "rows": self.plan_rows,
                "cost": self.plan_cost,
                "nodes": self.plan_nodes,
                "code": self.plan_code,
            },
            "duration_ms": self.duration_ms,
            "row_count": self.row_count,
            "returned": self.returned,
            "truncated": self.truncated,
            "sql_sha256": self.sql_sha256,
            "cache_status": self.cache_status,
            "sql_hidden": self.sql is None or not include_sql,
            "created_at": _isoformat(self.created_at),
        }
        if include_sql and self.sql is not None:
            payload["sql"] = self.sql
        return payload


def record_turn(
    settings: Settings,
    *,
    conversation_id: str,
    tenant_key: str,
    superset_user_id: int,
    outcome: str,
    stage: str,
    code: str = "",
    correlation_id: str = "",
    intent: str = "",
    dialect: str = "",
    tables: Any = None,
    inlined: Any = None,
    rls_applied: Any = None,
    plan: Any = None,
    duration_ms: int | None = None,
    row_count: int | None = None,
    returned: int | None = None,
    truncated: bool = False,
    cache_status: str = "",
    sql: str | None = None,
) -> AuditRecord:
    """Store one turn. Raises :class:`AuditInputError` on a malformed payload."""
    kind = (outcome or "").strip().lower()
    if kind not in OUTCOMES:
        raise AuditInputError(f"Unsupported outcome: {outcome}")
    where = (stage or "").strip().lower()
    if where not in STAGES:
        raise AuditInputError(f"Unsupported stage: {stage}")

    expression = (sql or "").strip()
    if len(expression) > MAX_SQL_CHARS:
        raise AuditInputError(f"sql must be at most {MAX_SQL_CHARS} characters")
    sealed = None
    fingerprint = ""
    if expression:
        box = SecretBox.from_root_secret(settings.service_secret)
        sealed = box.encrypt(expression).encode("utf-8")
        fingerprint = hashlib.sha256(expression.encode("utf-8")).hexdigest()

    estimate = plan if isinstance(plan, dict) else {}
    with connect(settings) as connection:
        with connection.cursor() as cursor:
            tenant_id = _tenant_id(cursor, tenant_key)
            if tenant_id is None:
                raise AuditInputError("Unknown tenant")
            cursor.execute(
                """
                INSERT INTO ai_sql_audit (
                    tenant_id, conversation_id, superset_user_id, correlation_id,
                    intent, outcome, stage, code, dialect, tables, inlined,
                    rls_applied, plan_source, plan_rows, plan_cost, plan_nodes,
                    plan_code, duration_ms, row_count, returned, truncated,
                    sql_ciphertext, sql_sha256, cache_status
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb,
                    %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                RETURNING id
                """,
                (
                    tenant_id,
                    conversation_id,
                    int(superset_user_id),
                    _text(correlation_id, MAX_CORRELATION_CHARS),
                    _text(intent, MAX_INTENT_CHARS),
                    kind,
                    where,
                    _text(code, MAX_CODE_CHARS),
                    _text(dialect, MAX_DIALECT_CHARS),
                    _json_list(tables, MAX_TABLES, MAX_TABLE_CHARS),
                    _json_list(inlined, MAX_LIST_ITEMS, MAX_ITEM_CHARS),
                    _json_list(rls_applied, MAX_LIST_ITEMS, MAX_ITEM_CHARS),
                    _optional_text(estimate.get("source"), 16),
                    _optional_int(estimate.get("rows")),
                    _optional_float(estimate.get("cost")),
                    _optional_int(estimate.get("nodes")),
                    _optional_text(estimate.get("code"), 32),
                    _optional_int(duration_ms),
                    _optional_int(row_count),
                    _optional_int(returned),
                    bool(truncated),
                    sealed,
                    fingerprint,
                    _cache_status(cache_status),
                ),
            )
            audit_id = str(cursor.fetchone()["id"])

    logger.info(
        "sql_audit outcome=%s stage=%s code=%s conversation=%s",
        kind,
        where,
        _text(code, MAX_CODE_CHARS) or "-",
        conversation_id,
    )
    return AuditRecord(
        id=audit_id,
        conversation_id=conversation_id,
        superset_user_id=int(superset_user_id),
        correlation_id=_text(correlation_id, MAX_CORRELATION_CHARS),
        intent=_text(intent, MAX_INTENT_CHARS),
        outcome=kind,
        stage=where,
        code=_text(code, MAX_CODE_CHARS),
        dialect=_text(dialect, MAX_DIALECT_CHARS),
        tables=tuple(_json_items(tables, MAX_TABLES, MAX_TABLE_CHARS)),
        inlined=tuple(_json_items(inlined, MAX_LIST_ITEMS, MAX_ITEM_CHARS)),
        rls_applied=tuple(_json_items(rls_applied, MAX_LIST_ITEMS, MAX_ITEM_CHARS)),
        plan_source=_optional_text(estimate.get("source"), 16),
        plan_rows=_optional_int(estimate.get("rows")),
        plan_cost=_optional_float(estimate.get("cost")),
        plan_nodes=_optional_int(estimate.get("nodes")),
        plan_code=_optional_text(estimate.get("code"), 32),
        duration_ms=_optional_int(duration_ms),
        row_count=_optional_int(row_count),
        returned=_optional_int(returned),
        truncated=bool(truncated),
        sql_sha256=fingerprint,
        cache_status=_cache_status(cache_status),
    )


def list_turns(
    settings: Settings,
    conversation_id: str,
    *,
    tenant_key: str,
    superset_user_id: int,
    limit: int = DEFAULT_LIST_LIMIT,
    include_sql: bool = False,
) -> list[AuditRecord]:
    """The turns of one conversation, newest first, owner-scoped."""
    size = max(1, min(int(limit or DEFAULT_LIST_LIMIT), MAX_LIST_LIMIT))
    with connect(settings) as connection:
        with connection.cursor() as cursor:
            tenant_id = _tenant_id(cursor, tenant_key)
            if tenant_id is None:
                return []
            cursor.execute(
                """
                SELECT *
                FROM ai_sql_audit
                WHERE tenant_id = %s
                  AND conversation_id = %s
                  AND superset_user_id = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (tenant_id, conversation_id, int(superset_user_id), size),
            )
            rows = cursor.fetchall()

    box = SecretBox.from_root_secret(settings.service_secret) if include_sql else None
    return [_record(row, box=box) for row in rows]


def _record(row: Any, *, box: SecretBox | None) -> AuditRecord:
    expression = None
    if box is not None and row.get("sql_ciphertext"):
        try:
            # The column stores the token's UTF-8 bytes; ``SecretBox`` speaks str.
            stored = bytes(row["sql_ciphertext"]).decode("utf-8")
            expression = box.decrypt(stored)
        except Exception:  # pragma: no cover - rotation or tampering
            logger.warning("Could not open a stored statement for audit %s", row["id"])
    return AuditRecord(
        id=str(row["id"]),
        conversation_id=str(row["conversation_id"]),
        superset_user_id=int(row["superset_user_id"]),
        correlation_id=row.get("correlation_id") or "",
        intent=row.get("intent") or "",
        outcome=row.get("outcome") or "",
        stage=row.get("stage") or "",
        code=row.get("code") or "",
        dialect=row.get("dialect") or "",
        tables=tuple(row.get("tables") or ()),
        inlined=tuple(row.get("inlined") or ()),
        rls_applied=tuple(row.get("rls_applied") or ()),
        plan_source=row.get("plan_source"),
        plan_rows=row.get("plan_rows"),
        plan_cost=row.get("plan_cost"),
        plan_nodes=row.get("plan_nodes"),
        plan_code=row.get("plan_code"),
        duration_ms=row.get("duration_ms"),
        row_count=row.get("row_count"),
        returned=row.get("returned"),
        truncated=bool(row.get("truncated")),
        sql_sha256=row.get("sql_sha256") or "",
        cache_status=row.get("cache_status") or "",
        sql=expression,
        created_at=row.get("created_at"),
    )


def _tenant_id(cursor: Any, tenant_key: str) -> Any:
    cursor.execute("SELECT id FROM ai_tenant WHERE key = %s", (tenant_key,))
    row = cursor.fetchone()
    return row["id"] if row else None


def _text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _cache_status(value: Any) -> str:
    """Only the four known states are stored; anything else becomes unknown."""
    status = str(value or "").strip().lower()
    return status if status in CACHE_STATUSES else ""


def _optional_text(value: Any, limit: int) -> str | None:
    text = _text(value, limit)
    return text or None


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _json_items(value: Any, limit: int, item_chars: int) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    items: list[str] = []
    for entry in value:
        text = str(entry or "").strip()[:item_chars]
        if text and text not in items:
            items.append(text)
        if len(items) >= limit:
            break
    return items


def _json_list(value: Any, limit: int, item_chars: int) -> str:
    return json.dumps(_json_items(value, limit, item_chars))


def _isoformat(value: datetime | None) -> str | None:
    return value.isoformat() if value else None
