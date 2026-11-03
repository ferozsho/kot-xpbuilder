"""Server-side re-validation of a statement that is about to run (P4-F09).

Execution happens inside Superset (it owns the analytics credentials), so the
service's job at that moment is narrow and strict: take the statement the client
wants to run, the authorized slice for this principal, and the target database,
and answer with **the** statement that may run — or refuse.

Re-validating here (rather than trusting the candidate produced moments earlier)
is deliberate: a statement can arrive from a replayed request, an older page, or a
hand-written client, and none of those earned a guard pass.
"""

from __future__ import annotations

from typing import Any

from xp_prompt import sql_guard
from xp_prompt.schema_slice import SchemaSlice


def resolve_dialect(
    schema: SchemaSlice,
    *,
    database: str | None = None,
    dialect: str | None = None,
) -> str:
    """Pick the dialect to parse with, refusing to guess between several.

    Raises :class:`xp_prompt.sql_guard.SqlPolicyError` with ``dialect_ambiguous``
    or ``unsupported_dialect``.
    """
    if dialect:
        normal = str(dialect).strip().lower()
        if normal not in sql_guard.SUPPORTED_DIALECTS:
            raise sql_guard.SqlPolicyError(
                "unsupported_dialect", f"unsupported SQL dialect: {dialect}"
            )
        return normal

    if database:
        for dataset in schema.datasets:
            if dataset.database == database:
                return dataset.dialect

    dialects = schema.dialects()
    if not dialects:
        raise sql_guard.SqlPolicyError(
            "unsupported_dialect", "no authorized tables are available"
        )
    if len(dialects) > 1:
        raise sql_guard.SqlPolicyError(
            "dialect_ambiguous", "the schema spans several dialects; choose one"
        )
    return dialects[0]


def validate(
    sql: str,
    *,
    schema: SchemaSlice,
    database: str | None = None,
    dialect: str | None = None,
    max_rows: int = sql_guard.MAX_ROWS,
) -> dict[str, Any]:
    """Return the statement that may be executed, or raise ``SqlPolicyError``."""
    resolved = resolve_dialect(schema, database=database, dialect=dialect)
    guarded = sql_guard.check(
        sql,
        dialect=resolved,
        allowed_tables=schema.allowed_tables(database=database),
        max_rows=max_rows,
        connection_names=schema.connection_names(),
        virtual_tables=schema.virtual_tables(database=database),
    )
    return {
        "sql": guarded.sql,
        "dialect": guarded.dialect,
        "tables": list(guarded.tables),
        "inlined": list(guarded.inlined),
        "limit": guarded.limit,
        "schema_fingerprint": schema.fingerprint,
    }
