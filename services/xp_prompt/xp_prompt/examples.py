"""Validated examples, scoped to a tenant and a dialect (P4-F03).

A second question "like one we already answered" is cheaper and more reliable to
answer when the earlier statement is in the prompt. This module is that library, and
its rules are mostly about what it must *not* do:

* **never cross a tenant.** Every read and write is filtered by the tenant row id, so
  one customer's question text cannot appear in another customer's prompt. There is no
  "shared" pool to leak from.
* **never cross a dialect or a schema.** Examples are stored with the schema
  fingerprint and matched per dialect, so a MySQL example cannot shape a PostgreSQL
  statement, and an example built on a different slice is not offered.
* **never store unvalidated SQL.** Only a statement that passed the guard *and* the
  semantic check is recorded.
* **the SQL is sealed** like every other statement (ADR-0016); only the question text
  is readable, and it is bounded.
* **retrieval is lexical, deliberately.** There is no embedding provider in this
  deployment, and a token-overlap score over a tenant's own small library is honest
  and explainable. A vector index can replace :func:`find_examples` without changing
  its callers.
* **best effort.** An example library outage degrades the prompt, never the turn.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from typing import Any

from xp_prompt.config import Settings
from xp_prompt.db import connect
from xp_prompt.security import SecretBox

logger = logging.getLogger(__name__)

MAX_QUESTION_CHARS = 500
MAX_SQL_CHARS = 20_000
MAX_TABLES = 20
#: How many recent examples a lookup considers before scoring them.
SCAN_LIMIT = 200
#: How many examples a prompt receives.
DEFAULT_LIMIT = 3
MAX_LIMIT = 5
#: Overlap below this is noise rather than precedent.
MIN_SCORE = 0.34
MAX_BLOCK_CHARS = 2_400

_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "how",
        "in",
        "is",
        "it",
        "of",
        "on",
        "per",
        "show",
        "the",
        "to",
        "was",
        "what",
        "which",
        "with",
    }
)
_WORD = re.compile(r"[a-z0-9_]+")


@dataclass(frozen=True, slots=True)
class Example:
    """One stored precedent: the question, and the statement that answered it."""

    question: str
    sql: str
    dialect: str
    score: float = 0.0


def tokens(text: str) -> set[str]:
    """Content words, so "how many students?" and "students count" can match."""
    return {
        word
        for word in _WORD.findall((text or "").lower())
        if len(word) > 2 and word not in _STOPWORDS
    }


def similarity(left: str, right: str) -> float:
    """Jaccard overlap over content words: explainable, and cheap enough here."""
    first, second = tokens(left), tokens(right)
    if not first or not second:
        return 0.0
    return len(first & second) / len(first | second)


def record_example(
    settings: Settings,
    *,
    tenant_key: str,
    question: str,
    sql: str,
    dialect: str,
    fingerprint: str,
    tables: Any = None,
) -> bool:
    """Store one validated precedent. Never raises: this is an optimisation."""
    text = " ".join((question or "").split())[:MAX_QUESTION_CHARS]
    statement = (sql or "").strip()
    if not text or not statement or len(statement) > MAX_SQL_CHARS:
        return False
    sealed = SecretBox.from_root_secret(settings.service_secret).encrypt(
        statement
    ).encode("utf-8")
    digest = hashlib.sha256(
        f"{dialect}|{text.lower()}|{statement}".encode("utf-8")
    ).hexdigest()
    try:
        with connect(settings) as connection:
            with connection.cursor() as cursor:
                tenant_id = _tenant_id(cursor, tenant_key)
                if tenant_id is None:
                    return False
                cursor.execute(
                    """
                    INSERT INTO ai_sql_example (
                        tenant_id, dialect, question, sql_ciphertext, sql_sha256,
                        schema_fingerprint, tables
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (tenant_id, dialect, sql_sha256) DO UPDATE
                        SET uses = ai_sql_example.uses + 1
                    """,
                    (
                        tenant_id,
                        (dialect or "").strip().lower()[:32],
                        text,
                        sealed,
                        digest,
                        (fingerprint or "")[:64],
                        _json_list(tables),
                    ),
                )
        return True
    except Exception as exc:  # pragma: no cover - best effort
        logger.info("sql_examples outcome=error kind=record reason=%s", type(exc).__name__)
        return False


def find_examples(
    settings: Settings,
    *,
    tenant_key: str,
    question: str,
    dialect: str,
    fingerprint: str = "",
    limit: int = DEFAULT_LIMIT,
) -> list[Example]:
    """The most similar precedents for this question, in this tenant and dialect."""
    size = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    try:
        with connect(settings) as connection:
            with connection.cursor() as cursor:
                tenant_id = _tenant_id(cursor, tenant_key)
                if tenant_id is None:
                    return []
                cursor.execute(
                    """
                    SELECT question, sql_ciphertext, dialect, schema_fingerprint
                    FROM ai_sql_example
                    WHERE tenant_id = %s AND dialect = %s
                    ORDER BY uses DESC, created_at DESC
                    LIMIT %s
                    """,
                    (tenant_id, (dialect or "").strip().lower(), SCAN_LIMIT),
                )
                rows = cursor.fetchall()
    except Exception as exc:  # pragma: no cover - best effort
        logger.info("sql_examples outcome=error kind=find reason=%s", type(exc).__name__)
        return []

    box = SecretBox.from_root_secret(settings.service_secret)
    scored: list[Example] = []
    for row in rows:
        trimmed = (fingerprint or "").strip()
        stored = (row.get("schema_fingerprint") or "").strip()
        if trimmed and stored and stored != trimmed:
            # A different slice: the example may reference columns this principal
            # cannot see, so it is not precedent.
            continue
        score = similarity(question, row.get("question") or "")
        if score < MIN_SCORE:
            continue
        try:
            statement = box.decrypt(bytes(row["sql_ciphertext"]).decode("utf-8"))
        except Exception:  # pragma: no cover - rotation or tampering
            continue
        scored.append(
            Example(
                question=row.get("question") or "",
                sql=statement,
                dialect=row.get("dialect") or "",
                score=round(score, 3),
            )
        )
    scored.sort(key=lambda example: example.score, reverse=True)
    logger.info("sql_examples outcome=found count=%d", len(scored[:size]))
    return scored[:size]


def prompt_block(examples: list[Example]) -> str:
    """The few-shot section a candidate prompt may include, or an empty string."""
    if not examples:
        return ""
    lines = [
        "Similar questions already answered on this data (verified statements, "
        "reuse their shape but not their assumptions):"
    ]
    total = len(lines[0])
    for index, example in enumerate(examples, start=1):
        entry = f"{index}. {example.question}\n   {example.sql}"
        if total + len(entry) > MAX_BLOCK_CHARS:
            break
        lines.append(entry)
        total += len(entry) + 1
    return "\n".join(lines)


def _tenant_id(cursor: Any, tenant_key: str) -> Any:
    cursor.execute("SELECT id FROM ai_tenant WHERE key = %s", (tenant_key,))
    row = cursor.fetchone()
    return row["id"] if row else None


def _json_list(value: Any) -> str:
    import json

    items: list[str] = []
    if isinstance(value, (list, tuple)):
        for entry in value:
            text = str(entry or "").strip()[:128]
            if text and text not in items:
                items.append(text)
            if len(items) >= MAX_TABLES:
                break
    return json.dumps(items)
