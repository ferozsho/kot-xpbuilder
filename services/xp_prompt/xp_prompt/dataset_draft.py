"""The dataset draft: a reload-safe proposal with lineage and a conflict rule (P6-F05/F08/F09).

A dataset is a durable object other people depend on, so the draft is durable too — and
it is deliberately *not* a Superset object yet. Between "the assistant proposed this"
and "a dataset exists" there are four things that must survive a page reload, a second
tab, and a failed save:

* **the proposal itself**, sealed (ADR-0016) because it carries a statement;
* **what was measured about it** — the quality report, so the operator can see why they
  were warned;
* **how it got here** — conversation, job, correlation, the statement's hash and the
  schema/policy fingerprints it was validated against;
* **which version is being saved** — every write is optimistic, so a stale tab is
  refused instead of silently overwriting (`DraftConflict`).

An ``idempotency_key`` makes creation replay-safe: the same key returns the same draft,
so a double-submitted "create" cannot produce two datasets (P6-F10's duplicate case).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from xp_prompt import dataset_spec
from xp_prompt.config import Settings
from xp_prompt.db import connect
from xp_prompt.security import SecretBox

logger = logging.getLogger(__name__)

DRAFT = "draft"
SAVED = "saved"
SCRAPPED = "scrapped"
STATUSES: tuple[str, ...] = (DRAFT, SAVED, SCRAPPED)

MAX_IDEMPOTENCY_CHARS = 96
MAX_CORRELATION_CHARS = 64
DEFAULT_LIST_LIMIT = 20
MAX_LIST_LIMIT = 100


class DraftNotFound(LookupError):
    """No such draft in this tenant, or not this user's."""


class DraftInputError(ValueError):
    """A rejected draft payload; ``code`` is stable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class DraftConflict(RuntimeError):
    """A write against a version that is no longer current (P6-F09)."""

    def __init__(self, expected: int, actual: int) -> None:
        super().__init__(
            f"draft has moved on: expected version {expected}, found {actual}"
        )
        self.expected = expected
        self.actual = actual


class DraftStateError(RuntimeError):
    """The draft is not in a state where this action makes sense."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class DatasetDraft:
    id: str
    name: str
    kind: str
    database: str
    status: str
    version: int
    spec: dict[str, Any]
    quality: dict[str, Any]
    quality_ok: bool
    fingerprint: str = ""
    schema_fingerprint: str = ""
    policy_version: str = ""
    sql_sha256: str = ""
    superset_dataset_id: int | None = None
    owners: tuple[str, ...] = ()
    conversation_id: str | None = None
    job_id: str | None = None
    correlation_id: str = ""
    superset_user_id: int = 0
    #: True when an idempotent create returned an existing draft instead of a new one.
    replayed: bool = False
    sql: str | None = field(default=None, repr=False)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    saved_at: datetime | None = None
    scrapped_at: datetime | None = None

    @property
    def is_open(self) -> bool:
        return self.status == DRAFT

    def to_public_dict(self, *, include_sql: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "database": self.database,
            "status": self.status,
            "version": self.version,
            "spec": dict(self.spec),
            "quality": dict(self.quality),
            "quality_ok": self.quality_ok,
            "fingerprint": self.fingerprint,
            "schema_fingerprint": self.schema_fingerprint,
            "policy_version": self.policy_version,
            "sql_sha256": self.sql_sha256,
            "superset_dataset_id": self.superset_dataset_id,
            "owners": list(self.owners),
            "conversation_id": self.conversation_id,
            "job_id": self.job_id,
            "correlation_id": self.correlation_id,
            "superset_user_id": self.superset_user_id,
            "replayed": self.replayed,
            "sql_hidden": self.sql is None or not include_sql,
            "created_at": _isoformat(self.created_at),
            "updated_at": _isoformat(self.updated_at),
            "saved_at": _isoformat(self.saved_at),
            "scrapped_at": _isoformat(self.scrapped_at),
        }
        if include_sql and self.sql is not None:
            payload["sql"] = self.sql
        return payload


def create_draft(
    settings: Settings,
    *,
    tenant_key: str,
    superset_user_id: int,
    spec: dataset_spec.DatasetSpec,
    quality: dict[str, Any],
    conversation_id: str | None = None,
    job_id: str | None = None,
    correlation_id: str = "",
    idempotency_key: str = "",
) -> DatasetDraft:
    """Store a draft, or return the one this idempotency key already created."""
    key = (idempotency_key or "").strip()[:MAX_IDEMPOTENCY_CHARS]
    with connect(settings) as connection:
        with connection.cursor() as cursor:
            tenant_id = _tenant_id(cursor, tenant_key)
            if key:
                existing = _by_idempotency(cursor, tenant_id, key)
                if existing is not None:
                    logger.info("dataset_draft outcome=replayed")
                    return _record(settings, existing, replayed=True)
            sealed = _seal(settings, spec.sql)
            cursor.execute(
                """
                INSERT INTO ai_dataset_draft (
                    tenant_id, conversation_id, job_id, superset_user_id,
                    correlation_id, name, kind, database_name, schema_name,
                    table_name, sql_ciphertext, sql_sha256, spec, spec_fingerprint,
                    schema_fingerprint, policy_version, quality, quality_ok,
                    idempotency_key, owners
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s::jsonb, %s, %s, %s, %s::jsonb, %s, %s, %s::jsonb)
                RETURNING *
                """,
                (
                    tenant_id,
                    conversation_id,
                    job_id,
                    int(superset_user_id),
                    (correlation_id or "")[:MAX_CORRELATION_CHARS],
                    spec.name,
                    spec.kind,
                    spec.database,
                    spec.schema,
                    spec.table,
                    sealed,
                    _sha256(spec.sql),
                    json.dumps(spec.to_public_dict(include_sql=False)),
                    spec.fingerprint,
                    spec.schema_fingerprint,
                    "",
                    json.dumps(quality or {}),
                    bool((quality or {}).get("ok")),
                    key,
                    json.dumps(list(spec.owners)),
                ),
            )
            row = cursor.fetchone()
    logger.info("dataset_draft outcome=created kind=%s", spec.kind)
    return _record(settings, row, replayed=False)


def get_draft(
    settings: Settings,
    draft_id: str,
    *,
    tenant_key: str,
    superset_user_id: int | None = None,
) -> DatasetDraft:
    with connect(settings) as connection:
        with connection.cursor() as cursor:
            tenant_id = _tenant_id(cursor, tenant_key)
            cursor.execute(
                "SELECT * FROM ai_dataset_draft WHERE tenant_id = %s AND id = %s",
                (tenant_id, draft_id),
            )
            row = cursor.fetchone()
    if row is None:
        raise DraftNotFound(draft_id)
    if superset_user_id is not None and int(row["superset_user_id"]) != int(
        superset_user_id
    ):
        # Ownership is not a filter a caller can talk around: a draft holds a query.
        raise DraftNotFound(draft_id)
    return _record(settings, row)


def list_drafts(
    settings: Settings,
    *,
    tenant_key: str,
    superset_user_id: int | None = None,
    status: str = "",
    conversation_id: str = "",
    limit: int = DEFAULT_LIST_LIMIT,
) -> list[DatasetDraft]:
    size = max(1, min(int(limit or DEFAULT_LIST_LIMIT), MAX_LIST_LIMIT))
    with connect(settings) as connection:
        with connection.cursor() as cursor:
            tenant_id = _tenant_id(cursor, tenant_key)
            clauses = ["tenant_id = %s"]
            values: list[Any] = [tenant_id]
            if superset_user_id is not None:
                clauses.append("superset_user_id = %s")
                values.append(int(superset_user_id))
            if status in STATUSES:
                clauses.append("status = %s")
                values.append(status)
            if conversation_id:
                clauses.append("conversation_id = %s")
                values.append(conversation_id)
            values.append(size)
            cursor.execute(
                "SELECT * FROM ai_dataset_draft WHERE "
                + " AND ".join(clauses)
                + " ORDER BY updated_at DESC LIMIT %s",
                tuple(values),
            )
            rows = cursor.fetchall()
    return [_record(settings, row) for row in rows]


def update_draft(
    settings: Settings,
    draft_id: str,
    *,
    tenant_key: str,
    superset_user_id: int,
    spec: dataset_spec.DatasetSpec,
    quality: dict[str, Any],
    expected_version: int | None = None,
) -> DatasetDraft:
    """Replace an open draft's proposal, refusing a stale writer (P6-F09)."""
    with connect(settings) as connection:
        with connection.cursor() as cursor:
            tenant_id = _tenant_id(cursor, tenant_key)
            current = _locked(cursor, tenant_id, draft_id, superset_user_id)
            if current["status"] != DRAFT:
                raise DraftStateError(
                    "not_open", f"a {current['status']} draft cannot be edited"
                )
            if expected_version is not None and int(current["version"]) != int(
                expected_version
            ):
                raise DraftConflict(int(expected_version), int(current["version"]))
            cursor.execute(
                """
                UPDATE ai_dataset_draft
                SET name = %s, kind = %s, database_name = %s, schema_name = %s,
                    table_name = %s, sql_ciphertext = %s, sql_sha256 = %s,
                    spec = %s::jsonb, spec_fingerprint = %s, schema_fingerprint = %s,
                    quality = %s::jsonb, quality_ok = %s, owners = %s::jsonb,
                    version = version + 1, updated_at = CURRENT_TIMESTAMP
                WHERE tenant_id = %s AND id = %s
                RETURNING *
                """,
                (
                    spec.name,
                    spec.kind,
                    spec.database,
                    spec.schema,
                    spec.table,
                    _seal(settings, spec.sql),
                    _sha256(spec.sql),
                    json.dumps(spec.to_public_dict(include_sql=False)),
                    spec.fingerprint,
                    spec.schema_fingerprint,
                    json.dumps(quality or {}),
                    bool((quality or {}).get("ok")),
                    json.dumps(list(spec.owners)),
                    tenant_id,
                    draft_id,
                ),
            )
            row = cursor.fetchone()
    logger.info("dataset_draft outcome=updated")
    return _record(settings, row)


def mark_saved(
    settings: Settings,
    draft_id: str,
    *,
    tenant_key: str,
    superset_user_id: int,
    dataset_id: int,
    owners: Any = None,
) -> DatasetDraft:
    """Pin the draft to the Superset dataset that now exists (P6-F08 lineage)."""
    if not dataset_id:
        raise DraftInputError("dataset_id_required", "a dataset id is required")
    with connect(settings) as connection:
        with connection.cursor() as cursor:
            tenant_id = _tenant_id(cursor, tenant_key)
            current = _locked(cursor, tenant_id, draft_id, superset_user_id)
            if current["status"] == SCRAPPED:
                raise DraftStateError(
                    "not_open", "a scrapped draft cannot be saved"
                )
            cursor.execute(
                """
                UPDATE ai_dataset_draft
                SET status = %s, superset_dataset_id = %s,
                    owners = %s::jsonb, saved_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE tenant_id = %s AND id = %s
                RETURNING *
                """,
                (
                    SAVED,
                    int(dataset_id),
                    json.dumps(_owners(owners) or list(current["owners"] or [])),
                    tenant_id,
                    draft_id,
                ),
            )
            row = cursor.fetchone()
    logger.info("dataset_draft outcome=saved")
    return _record(settings, row)


def scrap_draft(
    settings: Settings,
    draft_id: str,
    *,
    tenant_key: str,
    superset_user_id: int,
    reason: str = "",
) -> DatasetDraft:
    """Close a draft that produced nothing, so no half-object is left behind (F10)."""
    with connect(settings) as connection:
        with connection.cursor() as cursor:
            tenant_id = _tenant_id(cursor, tenant_key)
            current = _locked(cursor, tenant_id, draft_id, superset_user_id)
            if current["status"] == SCRAPPED:
                return _record(settings, current)
            cursor.execute(
                """
                UPDATE ai_dataset_draft
                SET status = %s, scrapped_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP,
                    spec = spec || %s::jsonb
                WHERE tenant_id = %s AND id = %s
                RETURNING *
                """,
                (SCRAPPED, json.dumps({"scrap_reason": (reason or "")[:64]}), tenant_id, draft_id),
            )
            row = cursor.fetchone()
    logger.info("dataset_draft outcome=scrapped")
    return _record(settings, row)


def _locked(
    cursor: Any, tenant_id: Any, draft_id: str, superset_user_id: int
) -> dict[str, Any]:
    cursor.execute(
        """
        SELECT * FROM ai_dataset_draft
        WHERE tenant_id = %s AND id = %s
        FOR UPDATE
        """,
        (tenant_id, draft_id),
    )
    row = cursor.fetchone()
    if row is None or int(row["superset_user_id"]) != int(superset_user_id):
        raise DraftNotFound(draft_id)
    return row


def _by_idempotency(
    cursor: Any, tenant_id: Any, key: str
) -> dict[str, Any] | None:
    cursor.execute(
        """
        SELECT * FROM ai_dataset_draft
        WHERE tenant_id = %s AND idempotency_key = %s
        """,
        (tenant_id, key),
    )
    return cursor.fetchone()


def _record(settings: Settings, row: dict[str, Any], *, replayed: bool = False) -> DatasetDraft:
    statement: str | None = None
    sealed = row.get("sql_ciphertext")
    if sealed:
        try:
            box = SecretBox.from_root_secret(settings.service_secret)
            statement = box.decrypt(bytes(sealed).decode("utf-8"))
        except Exception:  # pragma: no cover - rotation or tampering
            statement = None
    return DatasetDraft(
        id=str(row["id"]),
        name=str(row["name"]),
        kind=str(row["kind"]),
        database=str(row["database_name"]),
        status=str(row["status"]),
        version=int(row["version"]),
        spec=dict(row.get("spec") or {}),
        quality=dict(row.get("quality") or {}),
        quality_ok=bool(row.get("quality_ok")),
        fingerprint=str(row.get("spec_fingerprint") or ""),
        schema_fingerprint=str(row.get("schema_fingerprint") or ""),
        policy_version=str(row.get("policy_version") or ""),
        sql_sha256=str(row.get("sql_sha256") or ""),
        superset_dataset_id=(
            int(row["superset_dataset_id"])
            if row.get("superset_dataset_id") is not None
            else None
        ),
        owners=tuple(row.get("owners") or ()),
        conversation_id=(
            str(row["conversation_id"]) if row.get("conversation_id") else None
        ),
        job_id=str(row["job_id"]) if row.get("job_id") else None,
        correlation_id=str(row.get("correlation_id") or ""),
        superset_user_id=int(row.get("superset_user_id") or 0),
        replayed=replayed,
        sql=statement,
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
        saved_at=row.get("saved_at"),
        scrapped_at=row.get("scrapped_at"),
    )


def _seal(settings: Settings, sql: str) -> bytes | None:
    statement = (sql or "").strip()
    if not statement:
        return None
    box = SecretBox.from_root_secret(settings.service_secret)
    return box.encrypt(statement).encode("utf-8")


def _owners(value: Any) -> list[str]:
    owners: list[str] = []
    for entry in value if isinstance(value, (list, tuple)) else ():
        text = str(entry or "").strip()[:128]
        if text and text not in owners:
            owners.append(text)
        if len(owners) >= 10:
            break
    return owners


def _sha256(value: str) -> str:
    text = (value or "").strip()
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text else ""


def _tenant_id(cursor: Any, tenant_key: str) -> Any:
    cursor.execute("SELECT id FROM ai_tenant WHERE key = %s", (tenant_key,))
    row = cursor.fetchone()
    if row is None:
        raise DraftInputError("unknown_tenant", "tenant not found")
    return row["id"]


def _isoformat(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None
