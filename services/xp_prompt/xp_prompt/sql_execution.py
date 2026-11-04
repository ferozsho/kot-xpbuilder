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

from xp_prompt import settings_registry, sql_guard, sql_semantics
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
    limits: settings_registry.SqlLimits | None = None,
) -> dict[str, Any]:
    """Return the statement that may be executed, or raise ``SqlPolicyError``.

    ``limits`` is the deployment's safeguard set. When it is supplied the caller's
    ``max_rows`` can only narrow it — a request cannot raise the platform's row cap,
    and the metadata-schema rule is decided here rather than by the caller (P10-F06).
    The same limits are echoed in the result so the executor can apply the timeout
    and the concurrency gate instead of guessing them.
    """
    resolved = resolve_dialect(schema, database=database, dialect=dialect)
    cap = max_rows
    allow_metadata = False
    if limits is not None:
        cap = min(int(max_rows), limits.max_rows)
        allow_metadata = limits.allow_metadata_schemas
    guarded = sql_guard.check(
        sql,
        dialect=resolved,
        allowed_tables=schema.allowed_tables(database=database),
        max_rows=cap,
        connection_names=schema.connection_names(),
        virtual_tables=schema.virtual_tables(database=database),
        rls_targets=schema.rls_targets(database=database),
        allow_metadata_schemas=allow_metadata,
        extra_denied_functions=(
            limits.extra_denied_functions if limits is not None else None
        ),
    )
    # Semantics are re-checked here for the same reason the guard is: a statement
    # can arrive from a replay or a hand-written client, and "safe" is not the same
    # as "meaningful against this schema" (P4-F07).
    semantics = sql_semantics.validate(
        guarded.model_sql or guarded.sql, schema=schema, dialect=resolved
    )
    if not semantics.ok:
        raise sql_guard.SqlPolicyError(semantics.code, semantics.message())
    return {
        "sql": guarded.sql,
        "dialect": guarded.dialect,
        "tables": list(guarded.tables),
        "inlined": list(guarded.inlined),
        "rls_applied": list(guarded.rls_applied),
        "limit": guarded.limit,
        "limits": limits.to_public_dict() if limits is not None else None,
        "schema_fingerprint": schema.fingerprint,
    }


def validate_definition(
    sql: str,
    *,
    schema: SchemaSlice,
    database: str | None = None,
    dialect: str | None = None,
) -> dict[str, Any]:
    """Validate SQL that will be *stored* as a dataset definition (P6-F02).

    Same rules as :func:`validate` — one read-only statement, the allowlist, no
    denied functions, row-level scope — but no preview cap, because this text becomes
    the dataset rather than one page of its output. Nothing executes here.
    """
    resolved = resolve_dialect(schema, database=database, dialect=dialect)
    guarded = sql_guard.check(
        sql,
        dialect=resolved,
        allowed_tables=schema.allowed_tables(database=database),
        connection_names=schema.connection_names(),
        virtual_tables=schema.virtual_tables(database=database),
        rls_targets=schema.rls_targets(database=database),
        enforce_limit=False,
    )
    semantics = sql_semantics.validate(
        guarded.model_sql or guarded.sql, schema=schema, dialect=resolved
    )
    if not semantics.ok:
        raise sql_guard.SqlPolicyError(semantics.code, semantics.message())
    return {
        "sql": guarded.sql,
        "dialect": guarded.dialect,
        "tables": list(guarded.tables),
        "inlined": list(guarded.inlined),
        "rls_applied": list(guarded.rls_applied),
        "schema_fingerprint": schema.fingerprint,
    }
