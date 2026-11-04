"""The typed chart draft, and the lineage that makes it auditable (P7-F05/F09).

A recommendation is a suggestion; a draft is a *configuration*. The difference is that a
draft has been validated against the target visualization's control contract, carries the
rationale it came from, and remembers which dataset it was built on — so when it is saved
in Superset, "why does this chart exist and what produced it" is answerable from the row
rather than from the conversation.

The rules:

* **a draft is validated, not stored and hoped for.** Every write re-checks the form data
  against :mod:`chart_catalog`; a control the viz type does not read is a refusal, because
  a silently ignored control is a chart that lies about its configuration.
* **the draft is immutable in what it records.** ``viz_type``, ``form_data`` and the
  dataset it targets are replaced as a unit; the lineage (conversation, job, correlation,
  profile and catalog fingerprints, ranking source) is written once and kept.
* **writes are optimistic.** ``version`` is compared, so a stale tab cannot overwrite a
  newer edit (the same rule as dataset drafts, P6-F09).
* **nothing user-specific leaks into the row.** Form data is a configuration, not data:
  no filters containing values are stored beyond what the chart itself needs.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from xp_prompt import chart_catalog
from xp_prompt.config import Settings
from xp_prompt.db import connect

logger = logging.getLogger(__name__)

DRAFT = "draft"
SAVED = "saved"
SCRAPPED = "scrapped"
STATUSES: tuple[str, ...] = (DRAFT, SAVED, SCRAPPED)

MAX_TITLE_CHARS = 256
MAX_RATIONALE_CHARS = 600
MAX_CANDIDATE_ID_CHARS = 64
MAX_IDEMPOTENCY_CHARS = 96
MAX_CORRELATION_CHARS = 64
MAX_FORM_BYTES = 8_000
DEFAULT_LIST_LIMIT = 20
MAX_LIST_LIMIT = 100


class ChartDraftNotFound(LookupError):
    """No such draft in this tenant, or not this user's."""


class ChartDraftError(ValueError):
    """A refused draft; ``code`` is stable and content-free."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ChartDraftConflict(RuntimeError):
    """A write against a version that is no longer current."""

    def __init__(self, expected: int, actual: int) -> None:
        super().__init__(
            f"chart draft has moved on: expected version {expected}, found {actual}"
        )
        self.expected = expected
        self.actual = actual


@dataclass(frozen=True, slots=True)
class ChartDraft:
    """One validated chart configuration, before it exists in Superset."""

    id: str
    viz_type: str
    title: str
    dataset_id: int
    dataset_name: str
    database: str
    status: str
    version: int
    form_data: dict[str, Any]
    rationale: str = ""
    candidate_id: str = ""
    ranking_source: str = ""
    description: str = ""
    profile_fingerprint: str = ""
    catalog_fingerprint: str = ""
    conversation_id: str | None = None
    job_id: str | None = None
    correlation_id: str = ""
    superset_user_id: int = 0
    superset_slice_id: int | None = None
    run_count: int = 0
    replayed: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None
    saved_at: datetime | None = None
    scrapped_at: datetime | None = None

    @property
    def is_open(self) -> bool:
        return self.status == DRAFT

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "viz_type": self.viz_type,
            "title": self.title,
            "dataset_id": self.dataset_id,
            "dataset_name": self.dataset_name,
            "database": self.database,
            "status": self.status,
            "version": self.version,
            "form_data": dict(self.form_data),
            "rationale": self.rationale,
            "candidate_id": self.candidate_id,
            "ranking_source": self.ranking_source,
            "description": self.description,
            "profile_fingerprint": self.profile_fingerprint,
            "catalog_fingerprint": self.catalog_fingerprint,
            "conversation_id": self.conversation_id,
            "job_id": self.job_id,
            "correlation_id": self.correlation_id,
            "superset_user_id": self.superset_user_id,
            "superset_slice_id": self.superset_slice_id,
            "run_count": self.run_count,
            "replayed": self.replayed,
            "created_at": _isoformat(self.created_at),
            "updated_at": _isoformat(self.updated_at),
            "saved_at": _isoformat(self.saved_at),
            "scrapped_at": _isoformat(self.scrapped_at),
        }


def catalog_fingerprint(catalog: chart_catalog.Catalog) -> str:
    """A content-free identity for "these visualizations are available"."""
    material = {
        "available": sorted(catalog.available),
        "denied": sorted(catalog.denied),
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def validate_form(
    catalog: chart_catalog.Catalog, viz_type: str, form_data: Any
) -> dict[str, Any]:
    """The configuration that may be stored, or a refusal — never a best effort."""
    body = form_data if isinstance(form_data, dict) else {}
    ok, problems = catalog.validate(viz_type, body)
    if not ok:
        raise ChartDraftError(
            "invalid_configuration", ",".join(problems) or "unsupported_controls"
        )
    if len(json.dumps(body)) > MAX_FORM_BYTES:
        raise ChartDraftError("configuration_too_large", "the configuration is too large")
    return dict(body)


def create_draft(
    settings: Settings,
    *,
    tenant_key: str,
    superset_user_id: int,
    catalog: chart_catalog.Catalog,
    viz_type: str,
    title: str,
    dataset_id: int,
    dataset_name: str,
    database: str,
    form_data: Any,
    rationale: str = "",
    candidate_id: str = "",
    ranking_source: str = "",
    description: str = "",
    profile_fingerprint: str = "",
    conversation_id: str | None = None,
    job_id: str | None = None,
    correlation_id: str = "",
    idempotency_key: str = "",
) -> ChartDraft:
    """Store a validated draft, or return the one this idempotency key already made."""
    clean_title = " ".join((title or "").split())[:MAX_TITLE_CHARS]
    if not clean_title:
        raise ChartDraftError("title_required", "a chart title is required")
    if not dataset_id:
        raise ChartDraftError("dataset_required", "a dataset is required")
    body = validate_form(catalog, viz_type, form_data)
    key = (idempotency_key or "").strip()[:MAX_IDEMPOTENCY_CHARS]
    with connect(settings) as connection:
        with connection.cursor() as cursor:
            tenant_id = _tenant_id(cursor, tenant_key)
            if key:
                existing = _by_idempotency(cursor, tenant_id, key)
                if existing is not None:
                    logger.info("chart_draft outcome=replayed")
                    return _record(existing, replayed=True)
            cursor.execute(
                """
                INSERT INTO ai_chart_draft (
                    tenant_id, conversation_id, job_id, superset_user_id,
                    correlation_id, viz_type, title, dataset_id, dataset_name,
                    database_name, form_data, rationale, candidate_id,
                    ranking_source, description, profile_fingerprint,
                    catalog_fingerprint, idempotency_key
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s,
                        %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    tenant_id,
                    conversation_id,
                    job_id,
                    int(superset_user_id),
                    (correlation_id or "")[:MAX_CORRELATION_CHARS],
                    str(viz_type)[:64],
                    clean_title,
                    int(dataset_id),
                    str(dataset_name or "")[:128],
                    str(database or "")[:128],
                    json.dumps(body),
                    (rationale or "")[:MAX_RATIONALE_CHARS],
                    (candidate_id or "")[:MAX_CANDIDATE_ID_CHARS],
                    (ranking_source or "")[:16],
                    (description or "")[:MAX_TITLE_CHARS],
                    (profile_fingerprint or "")[:64],
                    catalog_fingerprint(catalog),
                    key,
                ),
            )
            row = cursor.fetchone()
    logger.info("chart_draft outcome=created viz_type=%s", viz_type)
    return _record(row)


def get_draft(
    settings: Settings,
    draft_id: str,
    *,
    tenant_key: str,
    superset_user_id: int | None = None,
) -> ChartDraft:
    with connect(settings) as connection:
        with connection.cursor() as cursor:
            tenant_id = _tenant_id(cursor, tenant_key)
            cursor.execute(
                "SELECT * FROM ai_chart_draft WHERE tenant_id = %s AND id = %s",
                (tenant_id, draft_id),
            )
            row = cursor.fetchone()
    if row is None:
        raise ChartDraftNotFound(draft_id)
    if superset_user_id is not None and int(row["superset_user_id"]) != int(
        superset_user_id
    ):
        raise ChartDraftNotFound(draft_id)
    return _record(row)


def list_drafts(
    settings: Settings,
    *,
    tenant_key: str,
    superset_user_id: int | None = None,
    status: str = "",
    conversation_id: str = "",
    dataset_id: int | None = None,
    limit: int = DEFAULT_LIST_LIMIT,
) -> list[ChartDraft]:
    size = max(1, min(int(limit or DEFAULT_LIST_LIMIT), MAX_LIST_LIMIT))
    clauses = ["tenant_id = %s"]
    with connect(settings) as connection:
        with connection.cursor() as cursor:
            tenant_id = _tenant_id(cursor, tenant_key)
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
            if dataset_id is not None:
                clauses.append("dataset_id = %s")
                values.append(int(dataset_id))
            values.append(size)
            cursor.execute(
                "SELECT * FROM ai_chart_draft WHERE "
                + " AND ".join(clauses)
                + " ORDER BY updated_at DESC LIMIT %s",
                tuple(values),
            )
            rows = cursor.fetchall()
    return [_record(row) for row in rows]


def update_draft(
    settings: Settings,
    draft_id: str,
    *,
    tenant_key: str,
    superset_user_id: int,
    catalog: chart_catalog.Catalog,
    viz_type: str,
    title: str,
    form_data: Any,
    rationale: str = "",
    expected_version: int | None = None,
) -> ChartDraft:
    """Replace an open draft's configuration, refusing a stale writer (P7-F07/F08)."""
    body = validate_form(catalog, viz_type, form_data)
    clean_title = " ".join((title or "").split())[:MAX_TITLE_CHARS]
    if not clean_title:
        raise ChartDraftError("title_required", "a chart title is required")
    with connect(settings) as connection:
        with connection.cursor() as cursor:
            tenant_id = _tenant_id(cursor, tenant_key)
            current = _locked(cursor, tenant_id, draft_id, superset_user_id)
            if current["status"] != DRAFT:
                raise ChartDraftError(
                    "not_open", f"a {current['status']} draft cannot be edited"
                )
            if expected_version is not None and int(current["version"]) != int(
                expected_version
            ):
                raise ChartDraftConflict(int(expected_version), int(current["version"]))
            cursor.execute(
                """
                UPDATE ai_chart_draft
                SET viz_type = %s, title = %s, form_data = %s::jsonb, rationale = %s,
                    catalog_fingerprint = %s, version = version + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE tenant_id = %s AND id = %s
                RETURNING *
                """,
                (
                    str(viz_type)[:64],
                    clean_title,
                    json.dumps(body),
                    (rationale or "")[:MAX_RATIONALE_CHARS],
                    catalog_fingerprint(catalog),
                    tenant_id,
                    draft_id,
                ),
            )
            row = cursor.fetchone()
    logger.info("chart_draft outcome=updated")
    return _record(row)


def record_run(
    settings: Settings,
    draft_id: str,
    *,
    tenant_key: str,
    superset_user_id: int,
) -> None:
    """Count one preview render, so the draft records how often it was looked at."""
    try:
        with connect(settings) as connection:
            with connection.cursor() as cursor:
                tenant_id = _tenant_id(cursor, tenant_key)
                cursor.execute(
                    """
                    UPDATE ai_chart_draft
                    SET run_count = run_count + 1, updated_at = CURRENT_TIMESTAMP
                    WHERE tenant_id = %s AND id = %s AND superset_user_id = %s
                    """,
                    (tenant_id, draft_id, int(superset_user_id)),
                )
    except Exception as exc:  # pragma: no cover - best effort
        logger.info("chart_draft outcome=error kind=record_run reason=%s", type(exc).__name__)


def mark_saved(
    settings: Settings,
    draft_id: str,
    *,
    tenant_key: str,
    superset_user_id: int,
    slice_id: int,
) -> ChartDraft:
    """Pin the draft to the Superset chart that now exists (P7-F09)."""
    if not slice_id:
        raise ChartDraftError("slice_id_required", "a chart id is required")
    with connect(settings) as connection:
        with connection.cursor() as cursor:
            tenant_id = _tenant_id(cursor, tenant_key)
            current = _locked(cursor, tenant_id, draft_id, superset_user_id)
            if current["status"] == SCRAPPED:
                raise ChartDraftError("not_open", "a scrapped draft cannot be saved")
            cursor.execute(
                """
                UPDATE ai_chart_draft
                SET status = %s, superset_slice_id = %s, saved_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE tenant_id = %s AND id = %s
                RETURNING *
                """,
                (SAVED, int(slice_id), tenant_id, draft_id),
            )
            row = cursor.fetchone()
    logger.info("chart_draft outcome=saved slice_id=%s", slice_id)
    return _record(row)


def scrap_draft(
    settings: Settings,
    draft_id: str,
    *,
    tenant_key: str,
    superset_user_id: int,
    reason: str = "",
) -> ChartDraft:
    """Close a draft that produced nothing (P7-F10's compensating action)."""
    with connect(settings) as connection:
        with connection.cursor() as cursor:
            tenant_id = _tenant_id(cursor, tenant_key)
            current = _locked(cursor, tenant_id, draft_id, superset_user_id)
            if current["status"] == SCRAPPED:
                return _record(current)
            cursor.execute(
                """
                UPDATE ai_chart_draft
                SET status = %s, scrapped_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP,
                    form_data = form_data || %s::jsonb
                WHERE tenant_id = %s AND id = %s
                RETURNING *
                """,
                (
                    SCRAPPED,
                    json.dumps({"scrap_reason": (reason or "")[:64]}),
                    tenant_id,
                    draft_id,
                ),
            )
            row = cursor.fetchone()
    logger.info("chart_draft outcome=scrapped")
    return _record(row)


def _locked(
    cursor: Any, tenant_id: Any, draft_id: str, superset_user_id: int
) -> dict[str, Any]:
    cursor.execute(
        """
        SELECT * FROM ai_chart_draft
        WHERE tenant_id = %s AND id = %s
        FOR UPDATE
        """,
        (tenant_id, draft_id),
    )
    row = cursor.fetchone()
    if row is None or int(row["superset_user_id"]) != int(superset_user_id):
        raise ChartDraftNotFound(draft_id)
    return row


def _by_idempotency(
    cursor: Any, tenant_id: Any, key: str
) -> dict[str, Any] | None:
    cursor.execute(
        "SELECT * FROM ai_chart_draft WHERE tenant_id = %s AND idempotency_key = %s",
        (tenant_id, key),
    )
    return cursor.fetchone()


def _record(row: dict[str, Any], *, replayed: bool = False) -> ChartDraft:
    return ChartDraft(
        id=str(row["id"]),
        viz_type=str(row["viz_type"]),
        title=str(row["title"]),
        dataset_id=int(row["dataset_id"]),
        dataset_name=str(row.get("dataset_name") or ""),
        database=str(row.get("database_name") or ""),
        status=str(row["status"]),
        version=int(row["version"]),
        form_data=dict(row.get("form_data") or {}),
        rationale=str(row.get("rationale") or ""),
        candidate_id=str(row.get("candidate_id") or ""),
        ranking_source=str(row.get("ranking_source") or ""),
        description=str(row.get("description") or ""),
        profile_fingerprint=str(row.get("profile_fingerprint") or ""),
        catalog_fingerprint=str(row.get("catalog_fingerprint") or ""),
        conversation_id=(
            str(row["conversation_id"]) if row.get("conversation_id") else None
        ),
        job_id=str(row["job_id"]) if row.get("job_id") else None,
        correlation_id=str(row.get("correlation_id") or ""),
        superset_user_id=int(row.get("superset_user_id") or 0),
        superset_slice_id=(
            int(row["superset_slice_id"])
            if row.get("superset_slice_id") is not None
            else None
        ),
        run_count=int(row.get("run_count") or 0),
        replayed=replayed,
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
        saved_at=row.get("saved_at"),
        scrapped_at=row.get("scrapped_at"),
    )


def _tenant_id(cursor: Any, tenant_key: str) -> Any:
    cursor.execute("SELECT id FROM ai_tenant WHERE key = %s", (tenant_key,))
    row = cursor.fetchone()
    if row is None:
        raise ChartDraftError("unknown_tenant", "tenant not found")
    return row["id"]


def _isoformat(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None
