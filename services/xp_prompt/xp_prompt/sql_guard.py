"""Read-only SQL guard (P4-F05).

Every candidate SQL string produced for a user passes through :func:`check`
before it can reach a database. The guard is deliberately paranoid and cheap:

* **one statement, and it must be a query.** ``sqlglot`` parses the text into an
  AST; anything that is not a ``SELECT``/``UNION`` (DDL, DML, ``COPY``, a vendor
  ``Command``, ``SELECT … INTO``) is rejected. A trailing ``; DROP …`` produces a
  second statement and is rejected before any node walk.
* **no escape hatches.** Functions that read files, run shells, sleep, reach the
  network, or advance sequences are denied by name, and system schemas
  (``information_schema``, ``pg_catalog``, ``mysql``, …) are refused outright.
* **only allowlisted tables.** When the caller knows the authorized schema slice
  (P4-F02), every referenced table must be in it — CTE names are not tables and
  are excluded.
* **a row cap the model cannot raise.** A missing or oversized ``LIMIT`` is
  replaced with :data:`MAX_ROWS`; only a non-literal limit (an expression or a
  bind) forces a subquery wrap, because its value cannot be checked here.

The guard never executes anything and never talks to a database: it is pure, so
the adversarial corpus in ``tests/test_sql_guard.py`` runs in milliseconds.

Refusals raise :class:`SqlPolicyError` with a stable ``code``. The code — not the
SQL text — is what gets stored and logged downstream (P4-F12).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

#: sqlglot logs the offending text ("<sql> contains unsupported syntax") when it
#: falls back to an opaque Command node. That would put candidate SQL — which may
#: contain user data — into the container log, so only real errors are kept.
logging.getLogger("sqlglot").setLevel(logging.ERROR)

#: Dialects this guard has been exercised against. Anything else is refused
#: rather than parsed optimistically with the wrong grammar.
SUPPORTED_DIALECTS: tuple[str, ...] = (
    "bigquery",
    "clickhouse",
    "duckdb",
    "mysql",
    "postgres",
    "redshift",
    "snowflake",
    "spark",
    "sqlite",
    "trino",
    "tsql",
)

MAX_SQL_CHARS = 20_000
MAX_ROWS = 1_000
MIN_ROWS = 1
#: A rendered row-level predicate is bounded before it is parsed or inlined.
MAX_RLS_CHARS = 2_000

#: Functions that read files, execute commands, reach the network, waste time, or
#: mutate state. Compared upper-cased against ``sql_name()``/anonymous names.
DENIED_FUNCTIONS: frozenset[str] = frozenset(
    {
        # filesystem / server-side readers
        "PG_READ_FILE",
        "PG_READ_BINARY_FILE",
        "PG_LS_DIR",
        "PG_STAT_FILE",
        "PG_LS_LOGDIR",
        "PG_LS_WALDIR",
        "PG_LS_TMPDIR",
        "LO_IMPORT",
        "LO_EXPORT",
        "LOAD_FILE",
        "READ_CSV",
        "READ_CSV_AUTO",
        "READ_PARQUET",
        "READ_JSON",
        "READ_JSON_AUTO",
        "READ_TEXT",
        "READ_BLOB",
        "DELTA_SCAN",
        "ICEBERG_SCAN",
        # network / cross-database
        "DBLINK",
        "DBLINK_EXEC",
        "POSTGRES_FDW",
        "OPENROWSET",
        "OPENDATASOURCE",
        "OPENDATASET",
        "URL",
        "S3",
        "GCS",
        "AZURE_BLOB_STORAGE",
        # command execution
        "XP_CMDSHELL",
        "SYS_EVAL",
        "SYS_EXEC",
        "SYSTEM",
        "SHELL",
        "EXEC",
        "EXECUTE",
        "EXECUTE_IMMEDIATE",
        "PYTHON",
        "RUN_PYTHON",
        # time / resource abuse
        "PG_SLEEP",
        "SLEEP",
        "BENCHMARK",
        "GET_LOCK",
        "RELEASE_LOCK",
        "PG_TERMINATE_BACKEND",
        "PG_CANCEL_BACKEND",
        "PG_RELOAD_CONF",
        # state mutation through a read-shaped call
        "NEXTVAL",
        "SETVAL",
        "SET_CONFIG",
        "LOAD_EXTENSION",
        "PG_CREATE_RESTORE_POINT",
        "PG_DROP_REPLICATION_SLOT",
    }
)

#: Schemas that describe the server rather than the tenant's data.
DENIED_SCHEMAS: frozenset[str] = frozenset(
    {
        "information_schema",
        "pg_catalog",
        "pg_toast",
        "pg_temp",
        "performance_schema",
        "mysql",
        "sys",
        "sqlite_master",
        "sqlite_schema",
        "system",
        "duckdb_internal",
        "iceberg",
        "snowflake",  # account-level INFORMATION_SCHEMA lives here too
        "dbms",
    }
)

#: MySQL/MariaDB executable comments hide a statement from naive scanners.
_EXECUTABLE_COMMENT = "/*!"

#: Node types that mutate, even if they appear inside a query-shaped statement.
_MUTATING_NODES: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.TruncateTable,
    exp.Merge,
    exp.Grant,
    exp.Command,
)

_QUERY_NODES: tuple[type[exp.Expression], ...] = (exp.Select, exp.Union, exp.Intersect, exp.Except)


class SqlPolicyError(RuntimeError):
    """A refusal with a stable, content-free ``code`` (safe to log and store)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class GuardResult:
    """What the guard hands to the executor.

    ``sql`` is the rewritten statement (limit enforced, normalized) — always
    re-rendered from the AST, never the caller's original text.
    """

    sql: str
    dialect: str
    tables: tuple[str, ...]
    limit: int
    wrapped: bool
    read_only: bool = True
    #: Datasets whose governed SQL was inlined as a CTE, for audit.
    inlined: tuple[str, ...] = ()
    #: Datasets a mandatory row-level predicate was applied to, for audit.
    rls_applied: tuple[str, ...] = ()
    #: The statement as the model wrote it, after name normalisation but before
    #: dataset definitions were inlined. Semantic validation (P4-F07) uses this: the
    #: inlined definition is the administrator's SQL, not the model's, and judging it
    #: against the dataset allowlist would refuse every virtual dataset.
    model_sql: str = ""

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "sql": self.sql,
            "dialect": self.dialect,
            "tables": list(self.tables),
            "limit": self.limit,
            "wrapped": self.wrapped,
            "read_only": self.read_only,
            "inlined": list(self.inlined),
            "rls_applied": list(self.rls_applied),
            "model_sql": self.model_sql,
        }


def check(
    sql: str,
    *,
    dialect: str = "postgres",
    allowed_tables: Any = None,
    max_rows: int = MAX_ROWS,
    connection_names: Any = None,
    virtual_tables: Any = None,
    rls_targets: Any = None,
    enforce_limit: bool = True,
    allow_metadata_schemas: bool = False,
    extra_denied_functions: Any = None,
) -> GuardResult:
    """Validate one candidate statement and return a safe, limit-enforced SQL.

    ``allowed_tables`` is an optional iterable of authorized ``table`` or
    ``schema.table`` names (lower-cased here). When supplied, an unknown reference
    is refused — that is how the authorized schema slice of P4-F02 is enforced.

    ``connection_names`` are Superset connection *labels* (``File uploads``). They
    are not SQL catalogs, so a reference that qualifies a table with one is
    rewritten to its ``schema.table`` form instead of being executed as a
    cross-database read — which PostgreSQL refuses with
    ``feature_not_supported``. Removing the label can only narrow the reference to
    the same authorized table, never widen it.

    ``virtual_tables`` maps a dataset name to its SQL for Superset *SQL datasets*:
    those have no physical table, so a reference to one is replaced by a CTE
    holding the governed query. The dataset SQL is admin-authored and is itself
    validated (one statement, a query, no denial) before it is inlined.

    ``rls_targets`` maps every reference form of a dataset with row-level security
    to ``(dataset name, mandatory predicate)``. Every reference is wrapped in a CTE
    that carries the predicate, so a model — or a caller replaying a statement —
    cannot omit or widen the scope: the predicate is applied by this function, not
    requested from the prompt. A clause that does not parse is refused rather than
    silently dropped.

    ``enforce_limit=False`` is for a **dataset definition** (P6-F02): the text is
    stored as a governed query and executed later by Superset, so a preview cap
    would become part of the dataset. Every other rule still applies, and previews
    of that dataset still run through the capped path.
    """
    text = (sql or "").strip()
    if not text:
        raise SqlPolicyError("empty", "SQL is empty")
    if len(text) > MAX_SQL_CHARS:
        raise SqlPolicyError("too_large", "SQL is too long")
    if _EXECUTABLE_COMMENT in text:
        raise SqlPolicyError("obfuscation", "executable comments are not allowed")

    normalised_dialect = (dialect or "").strip().lower()
    if normalised_dialect not in SUPPORTED_DIALECTS:
        raise SqlPolicyError(
            "unsupported_dialect", f"unsupported SQL dialect: {dialect}"
        )

    statements = _parse(text, normalised_dialect)
    if len(statements) != 1:
        raise SqlPolicyError(
            "multiple_statements", "exactly one statement is allowed"
        )
    statement = statements[0]
    if not isinstance(statement, _QUERY_NODES):
        raise SqlPolicyError("not_read_only", "only SELECT queries are allowed")

    _reject_mutating_nodes(statement)
    _reject_denied_functions(statement, extra_denied_functions)
    _strip_connection_catalog(statement, connection_names)
    definitions = _virtual_definitions(virtual_tables)
    inlined = _normalise_virtual_references(statement, definitions)
    # The allowlist is about what the model wrote, so it is checked before the
    # dataset definitions are inlined (their base tables are the admin's choice,
    # not the model's).
    tables = _collect_tables(statement)
    _reject_system_schemas(tables, allow_metadata=allow_metadata_schemas)
    if allowed_tables is not None:
        _reject_unlisted_tables(tables, allowed_tables)
    model_sql = statement.sql(dialect=normalised_dialect)
    scoped = _apply_dataset_scope(
        statement, definitions, _rls_definitions(rls_targets, normalised_dialect),
        normalised_dialect,
    )

    capped, limit, wrapped = (
        _enforce_limit(statement, max_rows)
        if enforce_limit
        else (statement, 0, False)
    )

    return GuardResult(
        sql=capped.sql(dialect=normalised_dialect),
        dialect=normalised_dialect,
        tables=tuple(sorted(selector for selector in tables)),
        limit=limit,
        wrapped=wrapped,
        read_only=True,
        inlined=inlined,
        rls_applied=scoped,
        model_sql=model_sql,
    )


def parse_query(text: str, dialect: str = "postgres") -> exp.Expression:
    """Parse exactly one statement, for callers that need the AST (P4-F07).

    Public so semantic validation can reuse the guard's parsing rules instead of
    inventing a second, subtly different one.
    """
    normalised = (dialect or "").strip().lower()
    if normalised not in SUPPORTED_DIALECTS:
        raise SqlPolicyError(
            "unsupported_dialect", f"unsupported SQL dialect: {dialect}"
        )
    statements = _parse((text or "").strip(), normalised)
    if len(statements) != 1:
        raise SqlPolicyError("multiple_statements", "exactly one statement is allowed")
    return statements[0]


def _parse(text: str, dialect: str) -> list[exp.Expression]:
    try:
        parsed = sqlglot.parse(text, read=dialect)
    except (SqlglotError, ValueError) as exc:
        raise SqlPolicyError("parse_error", "SQL could not be parsed") from exc
    # ``parse`` yields ``None`` for empty or comment-only fragments.
    statements = [item for item in parsed if item is not None]
    if not statements:
        raise SqlPolicyError("empty", "SQL is empty")
    return statements


def _reject_mutating_nodes(statement: exp.Expression) -> None:
    for node in statement.walk():
        if isinstance(node, _MUTATING_NODES):
            raise SqlPolicyError("not_read_only", "only SELECT queries are allowed")
        # ``SELECT … INTO new_table`` is a data-writing query in query clothing.
        if isinstance(node, exp.Into):
            raise SqlPolicyError("not_read_only", "SELECT INTO is not allowed")


def _reject_denied_functions(
    statement: exp.Expression, extra: Any = None
) -> None:
    """Refuse the built-in denials plus whatever the deployment added.

    ``sql.extra_denied_functions`` can only tighten: there is no setting that
    removes a built-in denial, so an operator cannot open a hole by configuration
    (P10-F06).
    """
    denied = DENIED_FUNCTIONS
    if extra:
        denied = denied | {str(name).strip().lower() for name in extra if str(name).strip()}
    for node in statement.walk():
        if isinstance(node, exp.Func):
            name = _function_name(node)
            if name and name in denied:
                raise SqlPolicyError(
                    "unsafe_function", "the query uses a forbidden function"
                )


def _function_name(node: exp.Expression) -> str:
    """Normalized function name, for the typed and the anonymous cases alike."""
    try:
        name = node.sql_name()
    except (AttributeError, ValueError):  # pragma: no cover - defensive
        name = ""
    if not name or name.upper() == "ANONYMOUS":
        raw = node.args.get("this")
        name = raw if isinstance(raw, str) else ""
    return name.strip().upper()


def _strip_connection_catalog(statement: exp.Expression, connection_names: Any) -> None:
    """Drop a Superset connection label from a table reference.

    A connection name is metadata, not a catalog: ``"File uploads".public.orders``
    is a cross-database reference and PostgreSQL answers ``feature_not_supported``.
    Models write it because the label appears next to the table in the prompt, so
    the reference is normalized rather than refused — the remaining
    ``schema.table`` still has to pass the allowlist.
    """
    labels = {
        str(name).strip().lower()
        for name in (connection_names or ())
        if str(name).strip()
    }
    if not labels:
        return
    for table in statement.find_all(exp.Table):
        catalog = table.args.get("catalog")
        if catalog is None:
            continue
        label = catalog if isinstance(catalog, str) else getattr(catalog, "name", "")
        if str(label).strip().lower() in labels:
            table.set("catalog", None)


def _virtual_definitions(virtual_tables: Any) -> dict[str, tuple[str, str]]:
    """Lower-cased dataset name -> (canonical name, SQL)."""
    if not isinstance(virtual_tables, dict) or not virtual_tables:
        return {}
    definitions: dict[str, tuple[str, str]] = {}
    for name, query in virtual_tables.items():
        original = str(name or "").strip()
        text = str(query or "").strip()
        if original and text:
            definitions[original.lower()] = (original, text)
    return definitions


def _normalise_virtual_references(
    statement: exp.Expression, definitions: dict[str, tuple[str, str]]
) -> tuple[str, ...]:
    """Point every reference to a virtual dataset at its bare dataset name.

    A virtual dataset has no schema or catalog, so ``public."Student Overview"``
    can only resolve once the qualifiers are gone. The name is rewritten to the
    dataset's canonical spelling as well: the reference and the CTE alias have to
    be identical, and a quoted identifier with a space is case-sensitive.
    """
    if not definitions:
        return ()
    found: list[str] = []
    for table in list(statement.find_all(exp.Table)):
        key = (table.name or "").lower()
        if not key or key not in definitions:
            continue
        table.set("catalog", None)
        table.set("db", None)
        table.set("this", exp.to_identifier(definitions[key][0], quoted=True))
        if key not in found:
            found.append(key)
    return tuple(sorted(found))


def _rls_definitions(rls_targets: Any, dialect: str) -> dict[str, tuple[str, str]]:
    """Validate the mandatory predicates Superset rendered for this principal.

    ``rls_targets`` is ``{reference form: (dataset name, predicate)}``. The
    predicate is SQL that Superset produced from its own RLS rules (identity and
    Jinja rendering stay there), so it is trusted in *intent* but still validated
    in *shape*: bounded, comment-free, and parseable as a condition. A policy that
    cannot be parsed is refused rather than dropped — silently dropping it would
    hand the caller the rows the policy exists to hide.
    """
    if not isinstance(rls_targets, dict) or not rls_targets:
        return {}
    validated: dict[str, tuple[str, str]] = {}
    for reference, value in rls_targets.items():
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            continue
        name = str(value[0] or "").strip()
        clause = str(value[1] or "").strip()
        if not name or not clause:
            continue
        if len(clause) > MAX_RLS_CHARS:
            raise SqlPolicyError("rls_invalid", "a row-level policy is too large")
        if _EXECUTABLE_COMMENT in clause:
            raise SqlPolicyError("rls_invalid", "a row-level policy is obfuscated")
        try:
            exp.condition(clause, dialect=dialect)
        except (SqlglotError, ValueError) as exc:
            raise SqlPolicyError(
                "rls_invalid", "a row-level policy could not be parsed"
            ) from exc
        validated[str(reference).strip().lower()] = (name, clause)
    return validated


def _apply_dataset_scope(
    statement: exp.Expression,
    definitions: dict[str, tuple[str, str]],
    scoped: dict[str, tuple[str, str]],
    dialect: str,
) -> tuple[str, ...]:
    """Reference every dataset through a CTE that carries its policy.

    Two things are folded into one pass because they are the same operation: a
    *virtual* dataset is a governed query that has to be inlined, and a dataset
    with row-level security has to be narrowed. Both become a CTE named after the
    dataset, so the generated SQL references the governed thing rather than the
    raw one — and the policy is applied by this function, never requested from the
    model.
    """
    planned: dict[str, tuple[str, exp.Expression]] = {}
    applied: list[str] = []
    for table in list(statement.find_all(exp.Table)):
        key = (table.name or "").lower()
        reference = ".".join(
            str(part).lower() for part in (table.catalog, table.db, key) if part
        )
        alias: str | None = None
        target = scoped.get(reference) or scoped.get(key)
        clause = target[1] if target else ""
        if key in definitions:
            canonical, query = definitions[key]
            body = _virtual_dataset_query(query, dialect)
            alias = canonical
            if clause:
                body = _narrow(body, clause)
        elif target:
            alias, clause = target
            # The CTE body is the original reference; the alias belongs to the
            # caller's expression and is preserved on the rewritten node.
            source = table.copy()
            source.set("alias", None)
            body = _narrow(source, clause)
        else:
            continue
        if alias.lower() not in planned:
            planned[alias.lower()] = (alias, body)
        if clause:
            applied.append(alias.lower())
        table.set("this", exp.to_identifier(alias, quoted=True))
        table.set("db", None)
        table.set("catalog", None)

    if not planned:
        return ()

    existing_cte_names = {
        cte.alias_or_name.lower()
        for cte in statement.find_all(exp.CTE)
        if cte.alias_or_name
    }
    for name in planned:
        if name in existing_cte_names:
            raise SqlPolicyError(
                "virtual_dataset_conflict",
                "a dataset name collides with a name the query already defines",
            )
    ctes = [
        exp.CTE(
            this=body,
            alias=exp.TableAlias(this=exp.to_identifier(alias, quoted=True)),
        )
        for alias, body in (planned[name] for name in sorted(planned))
    ]
    current = statement.args.get("with_")
    if isinstance(current, exp.With):
        # Dataset CTEs must come first: the query's own CTEs may reference them.
        current.set("expressions", [*ctes, *list(current.expressions)])
    else:
        statement.set("with_", exp.With(expressions=ctes, recursive=False))

    return tuple(sorted(set(applied)))


def _narrow(source: exp.Expression, clause: str) -> exp.Expression:
    """``source`` restricted to the mandatory predicate."""
    alias = exp.TableAlias(this=exp.to_identifier("xp_rls_source"))
    derived = exp.Subquery(this=source, alias=alias)
    return exp.select("*").from_(derived).where(exp.condition(clause))


def _virtual_dataset_query(query: str, dialect: str) -> exp.Expression:
    """The parsed dataset definition, or a refusal (never the raw text)."""
    text = query.strip()
    if len(text) > MAX_SQL_CHARS:
        raise SqlPolicyError("virtual_dataset_invalid", "a dataset query is too large")
    if _EXECUTABLE_COMMENT in text:
        raise SqlPolicyError("virtual_dataset_invalid", "a dataset query is obfuscated")
    parsed = _parse(text, dialect)
    if len(parsed) != 1:
        raise SqlPolicyError(
            "virtual_dataset_invalid", "a dataset query must be one statement"
        )
    definition = parsed[0]
    if not isinstance(definition, _QUERY_NODES):
        raise SqlPolicyError(
            "virtual_dataset_invalid", "a dataset query must be a query"
        )
    _reject_mutating_nodes(definition)
    _reject_denied_functions(definition)
    return definition


def _collect_tables(statement: exp.Expression) -> set[str]:
    """Referenced table names, excluding CTE aliases (they are not tables).

    A catalog-qualified reference keeps its catalog (``proj.ds.table`` in
    BigQuery), so a cross-project read cannot pass as the same dataset name. A
    connection label has already been removed by
    :func:`_strip_connection_catalog`, and an inlined virtual dataset is a CTE, so
    it is excluded like any other CTE name.
    """
    cte_names = {
        cte.alias_or_name.lower()
        for cte in statement.find_all(exp.CTE)
        if cte.alias_or_name
    }
    names: set[str] = set()
    for table in statement.find_all(exp.Table):
        name = (table.name or "").lower()
        if not name or name in cte_names:
            continue
        parts = [
            str(part).lower()
            for part in (table.catalog, table.db, name)
            if part
        ]
        names.add(".".join(parts))
    return names


def _reject_system_schemas(tables: set[str], *, allow_metadata: bool = False) -> None:
    """Metadata schemas describe the server, not the tenant's data.

    ``allow_metadata`` comes from ``sql.allow_metadata_schemas``, which is off by
    default and carries a platform ceiling: an operator may open it deliberately,
    and no caller may open it for one request.
    """
    if allow_metadata:
        return
    for table in tables:
        schema = table.split(".", 1)[0] if "." in table else ""
        if schema and schema in DENIED_SCHEMAS:
            raise SqlPolicyError(
                "system_schema", "metadata schemas are not available to analytics"
            )


def _reject_unlisted_tables(tables: set[str], allowed_tables: Any) -> None:
    allowed = {str(name).strip().lower() for name in allowed_tables}
    if not allowed:
        raise SqlPolicyError("unknown_table", "no tables are authorized")
    unknown = sorted(table for table in tables if table not in allowed)
    if unknown:
        raise SqlPolicyError("unknown_table", "the query references an unauthorized table")


def _enforce_limit(
    statement: exp.Expression, max_rows: int
) -> tuple[exp.Expression, int, bool]:
    """Guarantee a row cap the model cannot raise.

    Returns the rewritten statement, the effective cap, and whether a subquery
    wrap was needed (only when the existing limit is not a literal).
    """
    cap = max(MIN_ROWS, int(max_rows))
    existing = statement.args.get("limit")
    if existing is None:
        return statement.limit(cap), cap, False

    value = existing.expression
    if isinstance(value, exp.Literal) and not value.is_string:
        try:
            rows = int(value.this)
        except (TypeError, ValueError):
            rows = cap + 1
        if rows <= cap:
            return statement, rows, False
        return statement.limit(cap), cap, False

    # A bind or an expression: its value cannot be verified here, so contain the
    # whole statement instead of trusting it.
    wrapped = (
        exp.select("*")
        .from_(
            exp.Subquery(
                this=statement,
                alias=exp.TableAlias(this=exp.to_identifier("xp_capped")),
            )
        )
        .limit(cap)
    )
    return wrapped, cap, True
